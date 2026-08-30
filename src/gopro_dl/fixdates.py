"""`fix-dates`: make each file's capture date agree with GoPro's.

Runs entirely off the manifest and the files already on disk -- no API calls,
no media transferred. For every done file we compute the expected capture time
exactly the way `date_folder` does (`captured_at` resolved through
`captured_at_timezone`, falling back to `--timezone`), compare it with what the
file itself claims, and repair the difference.

Three things get corrected, in descending order of how much a photo library
cares:

1. **Missing Exif dates.** Most downloaded photos carry no DateTimeOriginal at
   all, so apps fall back to the file's modification time. `mediadates` adds
   the tags, which resizes the file (see the checksum note below).
2. **Wrong Exif/QuickTime dates**, overwritten in place.
3. **The modification time**, which after a download is when the download
   happened. Set to the capture instant for every file, including the ones
   whose container we cannot touch -- for those it is the only date an app has.

Adding Exif tags changes the file's size and bytes, so the stored S3 ETag and
expected size no longer describe it. Rather than leave `verify` reporting the
repaired file as corrupt -- and `verify --fix` deleting and re-downloading it,
silently undoing the repair -- the origin's size and hash are moved aside into
`origin_size`/`origin_checksum` and a local md5 takes over as what `verify
--deep` re-hashes against.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .logging_setup import log_event
from .manifest import Manifest
from .mediadates import (
    MalformedMedia,
    MediaDates,
    UnsupportedMedia,
    apply_dates,
    read_dates,
)
from .paths import CaptureDateError, parse_captured_at, resolve_timezone
from .verify import file_hash

DEFAULT_TOLERANCE = 120  # seconds


@dataclass
class FixReport:
    checked: int = 0
    fixed: list[tuple[str, str, str]] = field(default_factory=list)  # path, was, now
    added_tags: int = 0
    mtime_only: int = 0
    shifted: int = 0
    already_ok: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def problems(self) -> int:
        return len(self.failed)


def expected_times(
    captured_at: str, captured_at_timezone: str | None, fallback_timezone=None
) -> tuple[datetime, datetime]:
    """(capture-local wall time, UTC instant) for one item.

    The local half goes into Exif and is the same conversion that names the
    file's YYYY-MM-DD folder, which is why a repaired photo sorts into the day
    it is filed under. Pass the same `--timezone` the sync used, or the two will
    disagree for items GoPro gives no timezone for.
    """
    utc = parse_captured_at(captured_at)
    tz, _ = resolve_timezone(captured_at_timezone, fallback_timezone)
    return utc.astimezone(tz).replace(tzinfo=None), utc


def _off_by(current: datetime | None, expected: datetime, tolerance: int) -> bool:
    if current is None:
        return True
    return abs(current - expected) > timedelta(seconds=tolerance)


def needs_fix(
    dates: MediaDates,
    local: datetime,
    utc: datetime,
    tolerance: int,
    video_utc: bool = False,
) -> bool:
    """Does the file disagree with GoPro's answer, or say nothing at all?

    Fields flagged `ambiguous_clock` get one concession: cameras routinely write
    capture-*local* time into ISO-BMFF fields the spec calls UTC. That is a
    convention, not damage, so a value matching either reading is left alone
    unless `video_utc` says to normalise it.
    """
    if dates.can_add:
        return True
    utc_naive = utc.replace(tzinfo=None)
    saw_value = False
    for f in dates.fields:
        if f.current is None:
            continue
        saw_value = True
        expected = local if f.clock == "local" else utc_naive
        if not _off_by(f.current, expected, tolerance):
            continue
        if f.ambiguous_clock and not video_utc and not _off_by(f.current, local, tolerance):
            continue
        return True
    # Tags exist but are all blank or unparseable: worth filling in.
    return not saw_value and bool(dates.fields)


def _describe(dates: MediaDates) -> str:
    current = dates.primary
    if current:
        return current.strftime("%Y-%m-%d %H:%M:%S")
    return "no date" if dates.kind == "jpeg" else "none"


def _set_mtime(path: Path, utc: datetime) -> bool:
    """Point the file's mtime at the capture instant. Returns True if changed."""
    when = utc.timestamp()
    try:
        if abs(os.path.getmtime(path) - when) <= 1:
            return False
        os.utime(path, (when, when))
    except OSError:
        return False
    return True


@dataclass
class _Candidate:
    """One file that needs repair, and the times it should end up with."""

    row: object
    rel: str
    path: Path
    dates: MediaDates
    local: datetime
    utc: datetime
    folder: str
    shifted: bool = False


def _preserve_spacing(candidates: list[_Candidate], report: FixReport) -> None:
    """Correct a stopped camera clock by sliding a whole folder, not flattening it.

    A GoPro that loses power resets its clock, so a day's clips come back dated
    2015-01-01 -- but with the right *relative* times: 02:43, 03:05, 03:10. The
    API knows the true date, yet reports a single timestamp for every clip in
    the batch. Writing that verbatim would fix the date and destroy the
    ordering, collapsing a morning's filming onto one second.

    So when a group's embedded times are distinct, every clip in it moves by the
    one offset that lands the earliest clip on the API's time. Dates become
    right and the gaps between clips survive. Groups whose embedded times are
    already identical carry no ordering to protect and are left to the plain
    overwrite.
    """
    groups: dict[str, list[_Candidate]] = {}
    for c in candidates:
        if c.dates.kind == "mp4" and c.dates.primary is not None:
            groups.setdefault(c.folder, []).append(c)

    for group in groups.values():
        embedded = [c.dates.primary for c in group]
        if len(group) < 2 or len(set(embedded)) < 2:
            continue
        # The ISO-BMFF fields we rewrite are UTC, so anchor in that domain.
        offset = min(c.utc.replace(tzinfo=None) for c in group) - min(embedded)
        for c in group:
            skew = c.local - c.utc.replace(tzinfo=None)
            c.utc = (c.dates.primary + offset).replace(tzinfo=UTC)
            c.local = c.utc.replace(tzinfo=None) + skew
            c.shifted = True
        report.shifted += len(group)


def fix_dates(
    manifest: Manifest,
    dest: Path,
    fallback_timezone=None,
    tolerance: int = DEFAULT_TOLERANCE,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    video_utc: bool = False,
    set_mtime: bool = True,
    preserve_spacing: bool = True,
    on_progress=None,
) -> FixReport:
    report = FixReport()
    candidates: list[_Candidate] = []

    # Pass one: read what every file claims. Nothing is written yet, because
    # deciding a clock-reset group's new times needs the whole group first.
    for row in manifest.files_for_date_fix(since=since, until=until, limit=limit):
        rel = row["target_path"]
        path = dest / rel
        report.checked += 1

        if not path.exists():
            report.skipped.append((rel, "file is not on disk"))
            continue
        try:
            local, utc = expected_times(
                row["captured_at"], row["captured_at_timezone"], fallback_timezone
            )
        except CaptureDateError as exc:
            report.skipped.append((rel, f"no usable captured_at: {exc}"))
            continue

        try:
            dates = read_dates(path)
        except UnsupportedMedia as exc:
            # Nothing to embed dates in, but the mtime is still worth having.
            if not set_mtime:
                report.skipped.append((rel, str(exc)))
            elif dry_run or _set_mtime(path, utc):
                report.mtime_only += 1
            else:
                report.already_ok += 1
            continue
        except (MalformedMedia, OSError, ValueError) as exc:
            report.failed.append((rel, f"could not read dates: {exc}"))
            continue

        if dates.blocker and dates.missing:
            report.skipped.append((rel, dates.blocker))
            continue
        if not dates.fields and not dates.can_add:
            report.skipped.append((rel, "no date fields to rewrite"))
            continue

        if not needs_fix(dates, local, utc, tolerance, video_utc):
            if set_mtime and not dry_run and _set_mtime(path, utc):
                report.mtime_only += 1
            else:
                report.already_ok += 1
            continue

        candidates.append(_Candidate(row, rel, path, dates, local, utc, row["date_folder"]))

    if preserve_spacing:
        _preserve_spacing(candidates, report)

    # Pass two: write.
    for c in candidates:
        was = _describe(c.dates)
        now = c.local.strftime("%Y-%m-%d %H:%M:%S")
        if dry_run:
            report.fixed.append((c.rel, was, now))
            if c.dates.can_add:
                report.added_tags += 1
            if on_progress:
                on_progress(c.rel, was, now)
            continue

        try:
            result = apply_dates(c.path, c.local, c.utc, c.dates)
        except (MalformedMedia, OSError, ValueError) as exc:
            report.failed.append((c.rel, f"could not write dates: {exc}"))
            continue
        if not result.written:
            report.skipped.append((c.rel, "no field could be rewritten in place"))
            continue

        if result.rebuilt:
            report.added_tags += 1
        _rebase_integrity(manifest, c.row, c.path, digest=result.digest)
        touched_mtime = _set_mtime(c.path, c.utc) if set_mtime else False
        report.fixed.append((c.rel, was, now))
        log_event(
            logging.INFO,
            "dates_fixed",
            path=c.rel,
            was=was,
            now=now,
            fields=",".join(result.written),
            rebuilt=result.rebuilt,
            shifted=c.shifted,
            mtime=touched_mtime,
        )
        if on_progress:
            on_progress(c.rel, was, now)

    return report


def _rebase_integrity(manifest: Manifest, row, path: Path, digest: str | None = None) -> None:
    """Keep `verify` honest after we deliberately changed the bytes.

    The origin's size and checksum are preserved -- they still describe what
    GoPro holds -- but they no longer describe this file, so a local md5 and the
    new on-disk size take over as what verification checks against.

    `digest` is supplied when the caller already had the finished bytes in
    memory; otherwise the file is read back to hash it.
    """
    digest = digest or file_hash(path, "md5")
    if digest is None:
        return
    # Only the first repair sees the origin's values -- after that `checksum`
    # is already one of ours, and must not be mistaken for GoPro's.
    first_fix = row["dates_fixed_at"] is None
    manifest.record_date_fix(
        row["id"],
        local_checksum=digest,
        size=path.stat().st_size,
        origin_checksum=row["checksum"] if first_fix else None,
        origin_size=row["expected_size"] if first_fix else None,
    )
