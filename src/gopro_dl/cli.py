"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from rich.console import Console
from rich.table import Table

from .api import AuthExpired, GoProClient
from .auth import AuthGate, TokenError, TokenProvider
from .backfill import backfill_etags
from .circuit import CircuitBreaker
from .config import Config, load_config
from .logging_setup import log_event, setup_logging
from .manifest import Manifest
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

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--dest", help="destination directory (default ./downloads)")
        p.add_argument("--manifest-dir", help="where to keep manifest.db and logs")
        p.add_argument("--token", help="bearer token (prefer --token-file for long runs)")
        p.add_argument("--token-file", help="file holding the bearer token")
        p.add_argument("--user-id", help="gp_user_id, only needed for the cookie fallback")
        p.add_argument(
            "--timezone",
            help="timezone for folder dates when GoPro supplies none "
            "(IANA name like Europe/Brussels, or an offset like +02:00). "
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
    sync.add_argument("--create-dest", action="store_true", default=True, help=argparse.SUPPRESS)
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

    token = sub.add_parser("token", help="validate the current token")
    common(token)
    token.add_argument("--check", action="store_true", default=True)

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


def cmd_token(config: Config) -> int:
    tokens = TokenProvider(config.token, config.token_file)
    gate, shutdown = AuthGate(), threading.Event()
    with make_client(config, gate, shutdown, tokens) as client:
        account = client.validate_token()
    if account is None:
        console.print("[red]Token rejected.[/red] Copy a fresh one (see README) and retry.")
        return 1
    label = account.get("email") or account.get("id") or "authenticated"
    console.print(f"[green]Token OK[/green] - {label} (auth mode: {client.auth_mode})")
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
                console.print("[red]Token rejected.[/red] Run `gopro-dl token --check`.")
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
        }
        console.print(
            "Integrity: " + ", ".join(f"{labels.get(k, k)}={v}" for k, v in sorted(checks.items()))
        )

    skipped = manifest.skipped_items()
    if skipped:
        reasons: dict[str, int] = {}
        for row in skipped:
            reasons[row["skip_reason"] or "unknown"] = reasons.get(row["skip_reason"] or "unknown", 0) + 1
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


def cmd_retry(config: Config) -> int:
    with open_manifest(config) as manifest:
        items, files = manifest.reset_failed()
    console.print(f"Re-queued {files} files and {items} items. Run `gopro-dl sync`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    try:
        log_path = setup_logging(
            config.log_dir, quiet=config.quiet, verbose=getattr(args, "verbose", False)
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
    except TokenError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    except AuthExpired:
        console.print(
            "[red]GoPro rejected the token.[/red] Refresh it (see README) and "
            "check with `gopro-dl token --check`."
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
