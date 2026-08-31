"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .api import AuthExpired, GoProClient
from .auth import AuthGate, TokenError, TokenProvider, token_instructions
from .backfill import backfill_etags
from .browser_login import fetch_cached as fetch_cached_browser_token
from .browser_login import login as login_via_browser
from .circuit import CircuitBreaker
from .config import Config, apply_network_manifest_redirect, load_config
from .fixdates import DEFAULT_TOLERANCE, fix_dates
from .locations import AppDirs
from .logging_setup import log_event, setup_logging
from .manifest import Manifest
from .paths import parse_timezone
from .preflight import PreflightError, check_destination, check_disk_space, human_bytes
from .progress import DownloadProgress, NullProgress
from .runner import DownloadRunner, SyncStats, refresh_manifest
from .verify import verify as run_verify

console = Console()


# -- argument parsing ------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gopro-dl",
        description="Download your GoPro Plus cloud library in original quality, "
        "into flat YYYY-MM-DD folders, resumably.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, needs_manifest: bool = True) -> None:
        # needs_manifest steers the network-mount-detection check in main():
        # only commands that actually open the manifest (the default) should
        # pay for it. `token`/`setup` opt out explicitly below.
        p.set_defaults(needs_manifest=needs_manifest)
        p.add_argument("--dest", help="destination directory (default ~/Downloads/GoPro)")
        p.add_argument("--manifest-dir", help="where to keep manifest.db and logs")
        p.add_argument("--token", help="bearer token (prefer --token-file for long runs)")
        p.add_argument("--token-file", help="file holding the bearer token")
        p.add_argument("--user-id", help="gp_user_id, only needed for the cookie fallback")
        p.add_argument(
            "--timezone",
            help="timezone for folder dates when GoPro supplies none "
            "(IANA name like Europe/Paris, or an offset like +02:00). "
            "A timezone from the API always wins.",
        )
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--verbose", action="store_true")

    sync = sub.add_parser("sync", help="enumerate the library and download what is missing")
    common(sync)
    sync.add_argument("--concurrency", type=int, help="parallel downloads (default 3, max 8)")
    sync.add_argument("--limit", type=int, help="only process N media items (for smoke tests)")
    sync.add_argument("--since", help="only captures on/after this date (YYYY-MM-DD)")
    sync.add_argument("--until", help="only captures on/before this date (YYYY-MM-DD)")
    sync.add_argument("--types", help="comma-separated GoPro media types")
    sync.add_argument("--dry-run", action="store_true", help="plan only, download nothing")
    sync.add_argument("--retry-failed", action="store_true", help="re-queue failed files")
    sync.add_argument(
        "--no-manifest-refresh", action="store_true", help="skip the API enumeration pass"
    )
    sync.add_argument("--skip-preflight", action="store_true")
    sync.add_argument(
        "--non-interactive",
        action="store_true",
        help="on token expiry, poll the token file instead of prompting",
    )
    sync.add_argument("--max-attempts", type=int, default=4, help="per-file attempt budget")

    status = sub.add_parser("status", help="summarise manifest state")
    common(status)

    report = sub.add_parser("report", help="per-file detail")
    common(report)
    report.add_argument("--failed-only", action="store_true")
    report.add_argument("--csv", help="write CSV to this path")

    verify_p = sub.add_parser("verify", help="re-check downloaded files against the manifest")
    common(verify_p)
    verify_p.add_argument("--deep", action="store_true", help="re-hash where a checksum is known")
    verify_p.add_argument("--fix", action="store_true", help="re-queue anything that fails")

    retry = sub.add_parser("retry", help="reset failed files back to pending")
    common(retry)

    backfill = sub.add_parser(
        "backfill-etags",
        help="fetch origin checksums for files downloaded before content verification",
    )
    common(backfill)
    backfill.add_argument("--limit", type=int, help="only process N files")

    fix = sub.add_parser(
        "fix-dates",
        help="repair the capture dates embedded in downloaded photos and videos",
    )
    common(fix)
    fix.add_argument("--dry-run", action="store_true", help="report what would change only")
    fix.add_argument("--limit", type=int, help="only process N files")
    fix.add_argument("--since", help="only captures on/after this date (YYYY-MM-DD)")
    fix.add_argument("--until", help="only captures on/before this date (YYYY-MM-DD)")
    fix.add_argument(
        "--tolerance",
        type=int,
        default=DEFAULT_TOLERANCE,
        help=f"seconds of drift to accept before rewriting (default {DEFAULT_TOLERANCE})",
    )
    fix.add_argument(
        "--video-utc",
        action="store_true",
        help="also normalise video timestamps that hold capture-local time, "
        "which many cameras write in the field the spec calls UTC",
    )
    fix.add_argument(
        "--flatten-to-api",
        action="store_true",
        help="write GoPro's timestamp verbatim instead of sliding a folder whose "
        "camera clock was reset, which preserves the clips' relative times",
    )
    fix.add_argument(
        "--keep-mtime",
        action="store_true",
        help="leave file modification times alone (they default to the capture time)",
    )

    token = sub.add_parser("token", help="validate the current token")
    common(token, needs_manifest=False)

    setup = sub.add_parser(
        "setup",
        help="wizard: log into GoPro in a browser window (or paste a token), "
        "pick a destination, save settings",
    )
    common(setup, needs_manifest=False)
    setup.add_argument(
        "--force", action="store_true", help="overwrite an existing token file or config"
    )
    setup.add_argument(
        "--no-browser",
        action="store_true",
        help="skip the automated browser login and always prompt for the token",
    )

    return parser


# -- helpers ---------------------------------------------------------------


def open_manifest(config: Config) -> Manifest:
    manifest = Manifest(config.manifest_path)
    if not manifest.quick_check():
        raise PreflightError(f"manifest at {config.manifest_path} failed its integrity check")
    version = manifest.get_meta("schema_version")
    if version not in (None, "1"):
        raise PreflightError(f"manifest schema version {version} is newer than this tool")
    return manifest


def make_client(config: Config, gate: AuthGate, shutdown: threading.Event, tokens: TokenProvider):
    return GoProClient(
        tokens=tokens,
        gate=gate,
        breaker=CircuitBreaker(),
        user_id=config.user_id,
        concurrency=config.concurrency,
        shutdown=shutdown,
    )


def install_sigint(shutdown: threading.Event) -> None:
    state = {"count": 0}

    def handler(signum, frame):
        state["count"] += 1
        if state["count"] == 1:
            shutdown.set()
            console.print(
                "\n[yellow]Stopping after the current chunks. Partial files are kept; "
                "re-run to resume. Press Ctrl-C again to abort now.[/yellow]"
            )
        else:
            console.print("\n[red]Aborting.[/red]")
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handler)


# -- commands --------------------------------------------------------------


def _validate_raw_token(config: Config, token: str) -> tuple[dict | None, str]:
    gate, shutdown = AuthGate(), threading.Event()
    with make_client(config, gate, shutdown, TokenProvider(token=token)) as client:
        return client.validate_token(), client.auth_mode


def cmd_token(config: Config) -> int:
    tokens = TokenProvider(config.token, config.token_file)
    account, auth_mode = _validate_raw_token(config, tokens.token)
    if account is None:
        console.print("[red]Token rejected.[/red] Copy a fresh one (see README) and retry.")
        return 1
    label = account.get("email") or account.get("id") or "authenticated"
    console.print(f"[green]Token OK[/green] - {label} (auth mode: {auth_mode})")
    return 0


def _validate_token(config: Config, token: str) -> dict | None:
    account, _ = _validate_raw_token(config, token)
    return account


def _ask(prompt: str, default: str | None = "") -> str | None:
    """console.input(), falling back to `default` on Ctrl-C/Ctrl-D."""
    try:
        return console.input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return default


def _prompt_for_token() -> str | None:
    console.print(token_instructions("the prompt below"))
    token = _ask("\nPaste your gp_access_token value: ", default=None)
    if token is None:
        console.print("\n[yellow]Setup cancelled.[/yellow]")
    return token


def _detect_timezone() -> str | None:
    """Best-effort: the system's IANA timezone, e.g. "Europe/Paris".

    Reads the /etc/localtime symlink -- how macOS and most Linux distros
    represent the configured system timezone. Never raises: returns None if
    that link doesn't exist, isn't a symlink, or resolves to something that
    isn't actually a valid IANA zone.
    """
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return None
    _, sep, name = target.partition("zoneinfo/")
    if not sep:
        return None
    try:
        parse_timezone(name)
    except Exception:
        return None
    return name


def cmd_setup(config: Config, args) -> int:
    """First-run wizard: get/validate a token, pick a destination, save settings."""
    console.print("[bold]gopro-dl setup[/bold] - token, destination and settings in one pass.\n")

    token, source, account = config.token, ("--token" if config.token else None), None

    if not token and not args.no_browser:
        console.print("[dim]Checking for a saved GoPro browser session...[/dim]")
        cached = fetch_cached_browser_token(config.app_dirs.browser_profile)
        if cached and (account := _validate_token(config, cached)) is not None:
            token, source = cached, "a saved browser session"
        elif cached:
            console.print("[dim]That session has expired.[/dim]")

    if not token and not args.no_browser:
        fresh = login_via_browser(console, config.app_dirs.browser_profile)
        if fresh and (account := _validate_token(config, fresh)) is not None:
            token, source = fresh, "browser login"

    if not token:
        token = _prompt_for_token()
        if token is None:
            return 130
        source, account = "pasted", None

    if not token:
        console.print("[red]No token provided.[/red]")
        return 1

    if account is None:
        account = _validate_token(config, token)
    if account is None:
        console.print(
            "[red]That token was rejected by GoPro.[/red] Copy a fresh one and re-run `gopro-dl setup`."
        )
        return 1
    label = account.get("email") or account.get("id") or "authenticated"
    console.print(f"[green]Token OK[/green] - {label} (via {source})\n")

    token_file = config.token_file
    write_token = True
    if token_file.exists() and not args.force:
        answer = _ask(f"{token_file} already exists - overwrite? [y/N]: ", default="n")
        write_token = answer.lower() == "y"

    if write_token:
        parent_already_existed = token_file.parent.exists()
        token_file.parent.mkdir(parents=True, exist_ok=True)
        if not parent_already_existed:
            # Never chmod a directory we didn't create -- --token-file
            # ~/mytoken would otherwise chmod the user's actual home dir.
            token_file.parent.chmod(0o700)
        token_file.write_text(token + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        console.print(f"[green]Wrote[/green] {token_file} (chmod 600)")
    else:
        console.print(f"[dim]Left {token_file} untouched.[/dim]")

    dest = config.dest
    if not getattr(args, "dest", None):
        answer = _ask(f"Destination for media (Enter to accept {dest}): ")
        if answer:
            dest = Path(answer).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    console.print(f"Destination: {dest}")

    tz_name = getattr(args, "timezone", None) or ""
    if not tz_name:
        detected = _detect_timezone()
        if detected:
            console.print(f"[dim]Detected timezone: {detected} (override with --timezone if wrong)[/dim]")
            tz_name = detected
        else:
            tz_name = _ask(
                "Home timezone for folder dates, e.g. Europe/Paris (optional, Enter to skip): "
            )
    if tz_name:
        try:
            parse_timezone(tz_name)
        except Exception as exc:
            console.print(f"[yellow]Ignoring timezone {tz_name!r}: {exc}[/yellow]")
            tz_name = ""

    env_path = config.app_dirs.config_file
    if env_path.exists() and not args.force:
        console.print(
            f"\n[dim]{env_path} already exists - leaving it as is. Make sure it has:\n"
            f"  GOPRO_TOKEN_FILE={token_file}\n"
            f"  GOPRO_DEST={dest}"
            + (f"\n  GOPRO_TIMEZONE={tz_name}" if tz_name else "")
            + "[/dim]"
        )
    else:
        lines = [
            "# Written by `gopro-dl setup`. Same settings for every directory.\n",
            f"GOPRO_TOKEN_FILE={token_file}\n",
            f"GOPRO_DEST={dest}\n",
        ]
        if tz_name:
            lines.append(f"GOPRO_TIMEZONE={tz_name}\n")
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("".join(lines), encoding="utf-8")
        console.print(f"[green]Wrote[/green] {env_path}")

    console.print("\n[bold]Next:[/bold] gopro-dl sync --dry-run --limit 5")
    return 0


def cmd_sync(config: Config, args) -> int:
    shutdown = threading.Event()
    install_sigint(shutdown)
    gate = AuthGate()
    tokens = TokenProvider(config.token, config.token_file)
    stats = SyncStats()

    with open_manifest(config) as manifest, make_client(config, gate, shutdown, tokens) as client:
        if not args.skip_preflight:
            check_destination(config.dest, create=True)
            account = client.validate_token()
            if account is None:
                console.print("[red]Token rejected.[/red] Run `gopro-dl token` to check it.")
                return 1
            if client.user_id:
                manifest.set_meta("gopro_user_id", client.user_id)

        items_reset, files_reset = manifest.reset_stale()
        if items_reset or files_reset:
            console.print(
                f"[dim]Recovered {items_reset} items / {files_reset} files "
                f"from an interrupted run.[/dim]"
            )
        if args.retry_failed:
            ri, rf = manifest.reset_failed()
            console.print(f"[dim]Re-queued {rf} failed files ({ri} items).[/dim]")

        if not args.no_manifest_refresh:
            refresh_manifest(client, manifest, config, console, stats)

        rows = manifest.pending_items(
            since=args.since,
            until=args.until,
            limit=args.limit,
            max_attempts=args.max_attempts,
            types=config.types,
        )
        # Measured against what THIS run will fetch, so a --limit/--since run is
        # not blocked by the size of the whole library.
        remaining = manifest.bytes_for_items([row["id"] for row in rows])
        library_remaining = manifest.remaining_bytes(since=args.since, until=args.until)

        console.print()
        console.print(
            f"[bold]{len(rows)}[/bold] media items to fetch, "
            f"about [bold]{human_bytes(remaining)}[/bold] remaining."
        )
        if library_remaining > remaining:
            console.print(
                f"[dim]({human_bytes(library_remaining)} outstanding in total; "
                f"this run is narrowed by --limit/--since/--until.)[/dim]"
            )
        for warning, count in stats.tz_warnings.items():
            console.print(f"[yellow]note:[/yellow] {warning} ({count} items)")

        if args.dry_run:
            console.print("[cyan]--dry-run: nothing downloaded.[/cyan]")
            print_status(manifest, config)
            return 0
        if not rows:
            console.print("[green]Nothing to do - everything is already downloaded.[/green]")
            return 0

        if not args.skip_preflight:
            disk = check_disk_space(config.dest, remaining)
            if not disk.ok:
                console.print(
                    f"[red]Not enough free space:[/red] {human_bytes(disk.free)} free, "
                    f"{human_bytes(disk.required)} needed (incl. headroom).\n"
                    f"Narrow the run with --limit/--since/--until/--types, or free up space."
                )
                return 1

        # A chaptered recording is one item but several files, so the bar
        # counts the item_count hint rather than the number of items.
        expected_files = sum(max(int(row["item_count"] or 1), 1) for row in rows)
        progress = (
            NullProgress()
            if config.quiet
            else DownloadProgress(console, total_files=expected_files, total_bytes=remaining)
        )
        runner = DownloadRunner(
            client=client,
            manifest=manifest,
            config=config,
            console=console,
            tokens=tokens,
            gate=gate,
            shutdown=shutdown,
            progress=progress,
            stats=stats,
            max_attempts=args.max_attempts,
        )
        progress.start()
        try:
            runner.run(rows)
        except KeyboardInterrupt:
            shutdown.set()
        finally:
            progress.stop()

        return print_summary(manifest, config, stats, shutdown.is_set())


def print_summary(manifest: Manifest, config: Config, stats: SyncStats, interrupted: bool) -> int:
    console.print()
    console.print("[bold]Run summary[/bold]")
    console.print(
        f"  downloaded : {stats.files_done} files, {human_bytes(stats.bytes_downloaded)} "
        f"in {stats.elapsed / 60:.1f} min"
    )
    if stats.files_skipped:
        console.print(f"  skipped    : {stats.files_skipped} files")
    if stats.files_failed:
        console.print(f"  [red]failed     : {stats.files_failed} files[/red]")
    for media_id, filename, on_disk, listed in stats.size_mismatches:
        console.print(
            f"  [red]incomplete : {filename} ({media_id}) - {human_bytes(on_disk)} on disk, "
            f"{human_bytes(listed)} expected; re-run to refetch[/red]"
        )

    failures = manifest.failures(limit=20)
    failed_items = manifest.failed_items()
    if failures or failed_items:
        table = Table(title="Failures (see `gopro-dl report --failed-only` for all)")
        table.add_column("path / id")
        table.add_column("attempts", justify="right")
        table.add_column("error", overflow="fold")
        for row in failures:
            table.add_row(row["target_path"], str(row["attempts"]), row["last_error"] or "")
        for row in failed_items:
            table.add_row(f"item {row['id']}", str(row["attempts"]), row["last_error"] or "")
        console.print(table)

    if interrupted:
        console.print("[yellow]Interrupted - re-run `gopro-dl sync` to resume.[/yellow]")
        return 130
    return 1 if (stats.files_failed or failures or failed_items) else 0


def print_status(manifest: Manifest, config: Config) -> None:
    counts = manifest.counts()
    table = Table(title=f"Manifest: {manifest.path}")
    table.add_column("scope")
    table.add_column("state")
    table.add_column("count", justify="right")
    table.add_column("bytes", justify="right")
    for scope in ("items", "files"):
        for state, data in sorted(counts[scope].items()):
            table.add_row(scope, state, f"{data['n']:,}", human_bytes(data["bytes"]))
    console.print(table)

    checks = manifest.checksum_summary()
    if checks:
        labels = {
            "ok": "[green]content-verified[/green]",
            "mismatch": "[red]checksum mismatch[/red]",
            "unverified": "size-only (resumed file)",
            "not_checked": "size-only (downloaded before checksums)",
            "local_after_date_fix": "date-repaired (checked against the local copy)",
        }
        console.print(
            "Integrity: " + ", ".join(f"{labels.get(k, k)}={v}" for k, v in sorted(checks.items()))
        )

    skipped = manifest.skipped_items()
    if skipped:
        reasons: dict[str, int] = {}
        for row in skipped:
            reason = row["skip_reason"] or "unknown"
            reasons[reason] = reasons.get(reason, 0) + 1
        console.print("Skipped: " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())))
    console.print(f"Destination: {config.dest}")


def cmd_status(config: Config) -> int:
    with open_manifest(config) as manifest:
        print_status(manifest, config)
        failures = manifest.failures(limit=10)
        if failures:
            console.print(f"[red]{len(failures)} failed file(s) shown; see `report --failed-only`.[/red]")
    return 0


def cmd_report(config: Config, args) -> int:
    with open_manifest(config) as manifest:
        rows = manifest.failures() if args.failed_only else manifest.all_files()
        if args.csv:
            import csv

            with open(args.csv, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    ["media_id", "item_number", "target_path", "state", "expected_size",
                     "actual_size", "attempts", "last_error"]
                )
                for row in rows:
                    writer.writerow(
                        [row["media_id"], row["item_number"], row["target_path"], row["state"],
                         row["expected_size"], row["actual_size"], row["attempts"], row["last_error"]]
                    )
            console.print(f"Wrote {len(rows)} rows to {args.csv}")
            return 0

        table = Table(title="Files")
        for column in ("path", "state", "size", "attempts", "error"):
            table.add_column(column, overflow="fold")
        for row in rows[:200]:
            table.add_row(
                row["target_path"],
                row["state"],
                human_bytes(row["actual_size"] or row["expected_size"]),
                str(row["attempts"]),
                (row["last_error"] or "")[:80],
            )
        console.print(table)
        if len(rows) > 200:
            console.print(f"[dim]... and {len(rows) - 200} more (use --csv for the full list)[/dim]")
    return 0


def cmd_verify(config: Config, args) -> int:
    with open_manifest(config) as manifest:
        report = run_verify(manifest, config.dest, deep=args.deep, fix=args.fix)
    console.print(f"Checked {report.checked} files: [green]{report.ok} OK[/green]")
    for path in report.missing:
        console.print(f"  [red]missing[/red] {path}")
    for path, actual, expected in report.wrong_size:
        console.print(f"  [red]size[/red] {path}: {actual} != {expected}")
    for path in report.bad_checksum:
        console.print(f"  [red]checksum[/red] {path}")
    if report.unverifiable:
        console.print(f"  [yellow]{len(report.unverifiable)} file(s) had no size to check against[/yellow]")
    if report.problems and args.fix:
        console.print("[yellow]Re-queued the problem files; run `gopro-dl sync`.[/yellow]")
    return 1 if report.problems else 0


def cmd_backfill(config: Config, args) -> int:
    shutdown = threading.Event()
    gate = AuthGate()
    tokens = TokenProvider(config.token, config.token_file)

    with open_manifest(config) as manifest, make_client(config, gate, shutdown, tokens) as client:
        pending = manifest.files_needing_checksum(args.limit)
        if not pending:
            console.print("[green]Every downloaded file already has an origin checksum.[/green]")
            return 0
        console.print(
            f"Fetching origin checksums for [bold]{len(pending)}[/bold] file(s). "
            "No media is transferred."
        )
        report = backfill_etags(
            client,
            manifest,
            limit=args.limit,
            on_progress=lambda path, etag: console.print(f"  [dim]{etag}[/dim]  {path}"),
        )

    console.print(f"\n[green]{report.updated}[/green] checksum(s) recorded.")
    for path, actual, origin in report.size_mismatches:
        console.print(
            f"  [red]size differs from origin[/red] {path}: {actual:,} local vs {origin:,} at origin"
        )
    for path, error in report.failed:
        console.print(f"  [yellow]could not fetch[/yellow] {path}: {error}")
    if report.updated:
        console.print("Now run [bold]gopro-dl verify --deep[/bold] to check the bytes.")
    return 1 if (report.failed or report.size_mismatches) else 0


def cmd_fix_dates(config: Config, args) -> int:
    with open_manifest(config) as manifest:
        report = fix_dates(
            manifest,
            config.dest,
            fallback_timezone=config.fallback_timezone,
            tolerance=args.tolerance,
            since=args.since,
            until=args.until,
            limit=args.limit,
            dry_run=args.dry_run,
            video_utc=args.video_utc,
            set_mtime=not args.keep_mtime,
            preserve_spacing=not args.flatten_to_api,
            on_progress=lambda path, was, now: console.print(
                f"  [dim]{was}[/dim] -> [green]{now}[/green]  {path}"
            ),
        )

    verb = "would repair" if args.dry_run else "repaired"
    console.print(
        f"\nChecked {report.checked} file(s): [green]{len(report.fixed)} {verb}[/green], "
        f"{report.already_ok} already correct."
    )
    if report.added_tags:
        console.print(
            f"  {report.added_tags} photo(s) had no Exif date at all and "
            f"{'would get' if args.dry_run else 'now carry'} one."
        )
    if report.shifted:
        console.print(
            f"  {report.shifted} clip(s) had a reset camera clock; their folder is "
            "slid as a whole so the clips keep their order and spacing."
        )
    if report.mtime_only:
        console.print(f"  {report.mtime_only} file(s) needed only their modification time set.")
    for path, reason in report.skipped[:20]:
        console.print(f"  [yellow]skipped[/yellow] {path}: {reason}")
    if len(report.skipped) > 20:
        console.print(f"  [yellow]...and {len(report.skipped) - 20} more skipped[/yellow]")
    for path, error in report.failed:
        console.print(f"  [red]failed[/red] {path}: {error}")
    if report.fixed and not args.dry_run:
        console.print(
            "Repaired files no longer match the origin byte-for-byte by design; "
            "their checksums now describe the local copy."
        )
    return 1 if report.problems else 0


def cmd_retry(config: Config) -> int:
    with open_manifest(config) as manifest:
        items, files = manifest.reset_failed()
    console.print(f"Re-queued {files} files and {items} items. Run `gopro-dl sync`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args, AppDirs.resolve())
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    if args.needs_manifest:
        notice = apply_network_manifest_redirect(config)
        if notice:
            console.print(f"[yellow]{notice}[/yellow]")

    try:
        log_path = setup_logging(
            config.log_dir, console, quiet=config.quiet, verbose=getattr(args, "verbose", False)
        )
    except OSError as exc:
        console.print(f"[red]Cannot write logs to {config.log_dir}: {exc}[/red]")
        return 1

    log_event(logging.INFO, "run_start", command=args.command, dest=str(config.dest))
    if not config.quiet:
        console.print(f"[dim]log: {log_path}[/dim]")

    try:
        if args.command == "token":
            return cmd_token(config)
        if args.command == "setup":
            return cmd_setup(config, args)
        if args.command == "sync":
            return cmd_sync(config, args)
        if args.command == "status":
            return cmd_status(config)
        if args.command == "report":
            return cmd_report(config, args)
        if args.command == "verify":
            return cmd_verify(config, args)
        if args.command == "retry":
            return cmd_retry(config)
        if args.command == "backfill-etags":
            return cmd_backfill(config, args)
        if args.command == "fix-dates":
            return cmd_fix_dates(config, args)
    except TokenError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    except AuthExpired:
        console.print(
            "[red]GoPro rejected the token.[/red] Refresh it (see README) and "
            "check with `gopro-dl token`."
        )
        return 1
    except PreflightError as exc:
        console.print(f"[red]Pre-flight failed:[/red] {exc}")
        return 1
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted. Progress is saved; re-run to resume.[/yellow]")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
