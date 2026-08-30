"""Checks that run before a single byte is downloaded."""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .logging_setup import log_event

HEADROOM_FACTOR = 1.05
HEADROOM_BYTES = 5 * 1024**3


class PreflightError(RuntimeError):
    pass


@dataclass
class DiskReport:
    free: int
    required: int
    ok: bool


def human_bytes(n: float | None) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


def check_destination(dest: Path, create: bool = True) -> None:
    if dest.exists() and not dest.is_dir():
        raise PreflightError(f"destination {dest} exists but is not a directory")
    if not dest.exists():
        if not create:
            raise PreflightError(f"destination {dest} does not exist (pass --create-dest)")
        dest.mkdir(parents=True, exist_ok=True)
    probe = dest / ".gopro-dl-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise PreflightError(f"destination {dest} is not writable: {exc}") from exc


def _df_free_bytes(path: Path) -> int | None:
    """Free bytes according to `df`, which uses statfs64.

    Needed because macOS smbfs truncates statvfs block counts to 32 bits, so
    `shutil.disk_usage` under-reports any SMB volume larger than 4 TiB -- by
    exactly 2**32 blocks -- which would make the pre-flight refuse a run on a
    destination that has plenty of room.
    """
    try:
        out = subprocess.run(
            ["df", "-Pk", str(path)], capture_output=True, text=True, timeout=15, check=True
        ).stdout.strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    if len(out) < 2:
        return None
    # POSIX format: Filesystem 1024-blocks Used Available Capacity Mounted-on
    fields = out[-1].split()
    if len(fields) < 4 or not fields[3].isdigit():
        return None
    return int(fields[3]) * 1024


def free_space(dest: Path) -> int:
    """Free bytes at `dest`, preferring df and falling back to statvfs."""
    from_df = _df_free_bytes(dest)
    from_statvfs = shutil.disk_usage(dest).free
    if from_df is None:
        return from_statvfs
    if from_df > from_statvfs:
        # A large gap means statvfs wrapped; df is the trustworthy one.
        log_event(
            logging.DEBUG,
            "free_space_source",
            df=from_df,
            statvfs=from_statvfs,
            using="df",
        )
        return from_df
    return from_statvfs


def check_disk_space(dest: Path, required: int) -> DiskReport:
    free = free_space(dest)
    needed = int(required * HEADROOM_FACTOR) + HEADROOM_BYTES
    return DiskReport(free=free, required=needed, ok=free >= needed)
