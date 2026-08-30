"""Fetch origin ETags for files downloaded before content verification existed.

Costs one API call per media item plus one HEAD per file -- no media is
transferred. Once the hashes are stored, `verify --deep` can prove those files
byte-for-byte against S3 instead of only checking their size.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import httpx

from .api import ApiError, AuthExpired, GoProClient
from .logging_setup import log_event
from .manifest import Manifest
from .models import MediaItem, parse_download_response


@dataclass
class BackfillReport:
    updated: int = 0
    unchanged: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    size_mismatches: list[tuple[str, int, int]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.updated + self.unchanged + len(self.failed)


def backfill_etags(
    client: GoProClient,
    manifest: Manifest,
    limit: int | None = None,
    on_progress=None,
) -> BackfillReport:
    report = BackfillReport()
    rows = manifest.files_needing_checksum(limit)

    # One /download call serves every chapter of a recording.
    by_item: dict[str, list] = {}
    for row in rows:
        by_item.setdefault(row["media_id"], []).append(row)

    for media_id, file_rows in by_item.items():
        try:
            item = MediaItem.from_json(json.loads(file_rows[0]["raw_json"]))
            sources, skip = parse_download_response(client.get_download(media_id), item)
            if skip:
                for row in file_rows:
                    report.failed.append((row["target_path"], skip))
                continue
            by_number = {s.item_number: s for s in sources}
        except AuthExpired:
            raise
        except (ApiError, ValueError, KeyError) as exc:
            for row in file_rows:
                report.failed.append((row["target_path"], str(exc)))
            continue

        for row in file_rows:
            path = row["target_path"]
            source = by_number.get(row["item_number"])
            if source is None:
                report.failed.append((path, "chapter no longer present at the origin"))
                continue
            try:
                head = client.client.head(source.url, follow_redirects=True, timeout=30.0)
                head.raise_for_status()
            except httpx.HTTPError as exc:
                report.failed.append((path, f"HEAD failed: {exc}"))
                continue

            etag = (head.headers.get("ETag") or "").strip('"')
            if not etag:
                report.failed.append((path, "origin returned no ETag"))
                continue

            # Free cross-check while we are here: the origin's own size.
            length = head.headers.get("Content-Length")
            if length and length.isdigit() and row["actual_size"]:
                origin = int(length)
                if origin != row["actual_size"]:
                    report.size_mismatches.append((path, row["actual_size"], origin))

            manifest.set_checksum(row["id"], etag)
            report.updated += 1
            log_event(logging.INFO, "etag_backfilled", path=path, etag=etag)
            if on_progress:
                on_progress(path, etag)

    return report
