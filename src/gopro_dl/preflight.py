"""Checks that run before a single byte is downloaded."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .logging_setup import log_event

HEADROOM_FACTOR = 1.05
HEADROOM_BYTES = 5 * 1024**3

NETWORK_FS_TYPES = {"smbfs", "cifs", "smb3", "nfs", "nfs3", "nfs4", "afpfs", "webdav"}
_MACOS_MOUNT_RE = re.compile(r"^.+ on (?P<mount>/.*?) \((?P<type>[^,)]+)")


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
            raise PreflightError(f"destination {dest} does not exist")
        dest.mkdir(parents=True, exist_ok=True)
    probe = dest / ".gopro-dl-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise PreflightError(f"destination {dest} is not writable: {exc}") from exc


def _run_lines(cmd: list[str]) -> list[str] | None:
    """Run `cmd`, returning its stdout split into lines, or None on failure."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return out.strip().splitlines()


def _df_free_bytes(path: Path) -> int | None:
    """Free bytes according to `df`, which uses statfs64.

    Needed because macOS smbfs truncates statvfs block counts to 32 bits, so
    `shutil.disk_usage` under-reports any SMB volume larger than 4 TiB -- by
    exactly 2**32 blocks -- which would make the pre-flight refuse a run on a
    destination that has plenty of room.
    """
    out = _run_lines(["df", "-Pk", str(path)])
    if not out or len(out) < 2:
        return None
    # POSIX format: Filesystem 1024-blocks Used Available Capacity Mounted-on.
    # Anchored on the "NN%" capacity column rather than split by whitespace: an
    # SMB share -- the very case this function exists for -- is named something
    # like "//user@nas/Media Volume", and its spaces would shift the columns.
    match = re.search(r"\s(\d+)\s+(\d+)\s+(\d+)\s+\d+%", out[-1])
    if not match:
        return None
    return int(match.group(3)) * 1024


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


def _fs_type_macos(path: Path) -> str | None:
    """Parse `mount`, matching the mount point that's the most specific
    ancestor of `path` (i.e. has the most path segments)."""
    lines = _run_lines(["mount"])
    if lines is None:
        return None
    best_depth, best_type = -1, None
    for line in lines:
        m = _MACOS_MOUNT_RE.match(line)
        if not m:
            continue
        mount_point = Path(m["mount"])
        if mount_point != path and mount_point not in path.parents:
            continue
        depth = len(mount_point.parts)
        if depth > best_depth:
            best_depth, best_type = depth, m["type"]
    return best_type


def _fs_type_linux(path: Path) -> str | None:
    out = _run_lines(["df", "--output=fstype", str(path)])
    if not out or len(out) < 2:
        return None
    return out[-1].strip()


def is_network_filesystem(path: Path) -> bool:
    """Best-effort: is `path` (or its nearest existing ancestor) on a network
    mount (SMB/NFS/AFP/...)? Used to steer the manifest away from mounts that
    silently corrupt SQLite's WAL journal -- never to block a run, so any
    detection failure (command missing, unexpected output) just means False.
    """
    resolved = path.resolve()
    existing = next(p for p in (resolved, *resolved.parents) if p.exists())
    fstype = _fs_type_macos(existing) if sys.platform == "darwin" else _fs_type_linux(existing)
    return (fstype or "").lower() in NETWORK_FS_TYPES
