"""Configuration: CLI flag > env var > config file > default."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .locations import AppDirs, default_dest
from .models import DEFAULT_TYPES
from .paths import parse_timezone
from .preflight import is_network_filesystem

STATE_DIRNAME = ".gopro-dl"
MANIFEST_NAME = "manifest.db"
MAX_CONCURRENCY = 8


@dataclass
class Config:
    dest: Path
    app_dirs: AppDirs
    token: str | None = None
    token_file: Path | None = None
    user_id: str | None = None
    concurrency: int = 3
    types: tuple[str, ...] = DEFAULT_TYPES
    manifest_dir: Path | None = None
    non_interactive: bool = False
    quiet: bool = False
    fallback_timezone: object = None

    @property
    def state_dir(self) -> Path:
        return self.manifest_dir or (self.dest / STATE_DIRNAME)

    @property
    def manifest_path(self) -> Path:
        return self.state_dir / MANIFEST_NAME

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _expand(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None


def load_config(args, app_dirs: AppDirs) -> Config:
    """Build a Config from parsed CLI args, environment and the config file.

    `app_dirs` is resolved once by the caller (cli.py: main()) -- this
    function never decides where gopro-dl's own state lives, only where a
    single run's settings come from.
    """
    load_dotenv(app_dirs.config_file, override=False)

    dest = _expand(getattr(args, "dest", None) or _env("GOPRO_DEST")) or default_dest()
    token_file = (
        _expand(getattr(args, "token_file", None) or _env("GOPRO_TOKEN_FILE"))
        or app_dirs.token_file
    )

    concurrency = getattr(args, "concurrency", None)
    if concurrency is None:
        raw = _env("GOPRO_CONCURRENCY")
        concurrency = int(raw) if raw and raw.isdigit() else 3
    concurrency = max(1, min(int(concurrency), MAX_CONCURRENCY))

    raw_types = getattr(args, "types", None)
    types = (
        tuple(t.strip() for t in raw_types.split(",") if t.strip())
        if raw_types
        else DEFAULT_TYPES
    )

    tz_name = getattr(args, "timezone", None) or _env("GOPRO_TIMEZONE")
    fallback_tz = None
    if tz_name:
        try:
            fallback_tz = parse_timezone(tz_name)
        except Exception as exc:
            raise ValueError(
                f"invalid --timezone {tz_name!r}: {exc}. "
                "Use an IANA name like Europe/Brussels, or an offset like +02:00."
            ) from exc

    return Config(
        dest=dest,
        app_dirs=app_dirs,
        fallback_timezone=fallback_tz,
        token=getattr(args, "token", None) or _env("GOPRO_TOKEN"),
        token_file=token_file,
        user_id=getattr(args, "user_id", None) or _env("GOPRO_USER_ID"),
        concurrency=concurrency,
        types=types,
        manifest_dir=_expand(
            getattr(args, "manifest_dir", None) or _env("GOPRO_MANIFEST_DIR")
        ),
        non_interactive=bool(getattr(args, "non_interactive", False)),
        quiet=bool(getattr(args, "quiet", False)),
    )


def apply_network_manifest_redirect(config: Config) -> str | None:
    """If `dest` looks like a network mount and no --manifest-dir was given,
    point the manifest and logs at a local per-user location instead --
    SMB/NFS can silently corrupt SQLite's WAL journal. Mutates `config` in
    place; returns a notice to display, or None if nothing changed.
    """
    if config.manifest_dir is not None:
        return None
    if (config.dest / STATE_DIRNAME / MANIFEST_NAME).exists():
        # Already colocated from a previous run -- never split an established
        # manifest by redirecting out from under it, and skip the mount/df
        # subprocess call on what's presumably the common, fast, repeated path
        # (status/report on an already-running sync).
        return None
    if not is_network_filesystem(config.dest):
        return None
    config.manifest_dir = config.app_dirs.manifest_dir_for(config.dest)
    return (
        f"{config.dest} looks like a network mount -- keeping the manifest and logs "
        f"locally at {config.manifest_dir} instead (SMB/NFS can corrupt SQLite's WAL "
        f"journal). Override with --manifest-dir."
    )
