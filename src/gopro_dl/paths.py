"""Capture-date folder naming and deterministic collision resolution.

Pure functions only -- everything here is unit tested without touching the
network or the filesystem.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# "+02:00", "-0700", "Z"
_OFFSET_RE = re.compile(r"^(?P<sign>[+-])(?P<h>\d{2}):?(?P<m>\d{2})$")
_UNSAFE = re.compile(r"[/\x00]")


class CaptureDateError(ValueError):
    """captured_at was missing or unparseable."""


def parse_captured_at(value: str) -> datetime:
    """Parse the API's captured_at into an aware UTC datetime."""
    if not value:
        raise CaptureDateError("captured_at is empty")
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CaptureDateError(f"unparseable captured_at: {value!r}") from exc
    if dt.tzinfo is None:
        # The API documents UTC; a naive timestamp is treated as such.
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_timezone(text: str) -> timezone | ZoneInfo:
    """Parse an IANA name ("Europe/Brussels") or a UTC offset ("+02:00")."""
    text = text.strip()
    match = _OFFSET_RE.match(text)
    if match:
        delta = timedelta(hours=int(match["h"]), minutes=int(match["m"]))
        if match["sign"] == "-":
            delta = -delta
        return timezone(delta)
    return ZoneInfo(text)  # raises ZoneInfoNotFoundError on a bad name


def resolve_timezone(
    raw: str | None, fallback: timezone | ZoneInfo | None = None
) -> tuple[timezone | ZoneInfo, str | None]:
    """Resolve captured_at_timezone into a tzinfo.

    The field's real shape is account-dependent (IANA name, UTC offset, or --
    commonly -- absent). A value from the API always wins; `fallback` is used
    only when there is none, which is what makes clips shot around midnight
    land in the right local day.

    Returns (tzinfo, warning); warning is non-None when the fallback was used.
    """
    default = fallback or UTC
    label = getattr(default, "key", None) or str(default)

    if raw is None or not str(raw).strip():
        return default, f"no captured_at_timezone; using {label}"
    text = str(raw).strip()
    try:
        return parse_timezone(text), None
    except (ZoneInfoNotFoundError, ValueError):
        return default, f"unrecognised captured_at_timezone {text!r}; using {label}"


def date_folder(
    captured_at: str,
    captured_at_timezone: str | None = None,
    fallback_timezone: timezone | ZoneInfo | None = None,
) -> tuple[str, str | None]:
    """Return (YYYY-MM-DD folder name in capture-local time, warning or None)."""
    utc_dt = parse_captured_at(captured_at)
    tz, warning = resolve_timezone(captured_at_timezone, fallback_timezone)
    return utc_dt.astimezone(tz).strftime("%Y-%m-%d"), warning


def safe_filename(filename: str, fallback: str) -> str:
    """Strip path separators; fall back when the API gives us nothing usable."""
    cleaned = _UNSAFE.sub("_", (filename or "").strip())
    cleaned = cleaned.lstrip(".")
    return cleaned or fallback


def split_ext(filename: str) -> tuple[str, str]:
    return os.path.splitext(filename)


def suffixed(filename: str, media_id: str, length: int = 6) -> str:
    """Deterministic collision name: GX010123.MP4 -> GX010123_a1b2c3.MP4.

    Derived only from the immutable media id, so the same file always resolves
    to the same path across runs -- which is what keeps resume matching.
    """
    stem, ext = split_ext(filename)
    return f"{stem}_{media_id[:length]}{ext}"


def target_path(
    folder: str,
    filename: str,
    media_id: str,
    owner_of,
    item_number: int = 1,
    chapter_count: int = 1,
) -> str:
    """Assign a collision-free path relative to the destination directory.

    `owner_of(relpath) -> media_id | None` says which media item already claims
    a path (ignoring this file's own row). Who the owner is decides the fix:

    * a sibling chapter of the same recording -> suffix the chapter number,
      which reads naturally (GX010001_p02.MP4)
    * a different media item -> suffix this item's id, which is immutable and
      therefore stable across runs, so resume keeps matching the same file
    """
    candidate = f"{folder}/{filename}"
    owner = owner_of(candidate)
    if owner is None:
        return candidate

    if owner == media_id and chapter_count > 1:
        stem, ext = split_ext(filename)
        candidate = f"{folder}/{stem}_p{item_number:02d}{ext}"
        if owner_of(candidate) is None:
            return candidate

    for length in (6, 12, 24):
        candidate = f"{folder}/{suffixed(filename, media_id, length)}"
        if owner_of(candidate) is None:
            return candidate

    # Same id, same filename, same folder, three times over: refuse rather
    # than silently overwrite media.
    raise RuntimeError(f"cannot find a free path for {folder}/{filename} ({media_id})")
