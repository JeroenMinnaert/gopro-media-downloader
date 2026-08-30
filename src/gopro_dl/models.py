"""Typed views over the GoPro API's JSON."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# Everything the user wants backed up. MultiClipEdit is deliberately absent:
# those are GoPro-generated edits, not originals.
DEFAULT_TYPES = (
    "Video",
    "Photo",
    "Burst",
    "BurstVideo",
    "Continuous",
    "LoopedVideo",
    "TimeLapse",
    "TimeLapseVideo",
)

EXCLUDED_TYPES = frozenset({"MultiClipEdit"})

SEARCH_FIELDS = (
    "camera_model,captured_at,content_title,content_type,created_at,gopro_user_id,"
    "gopro_media,filename,file_extension,file_size,height,fov,id,item_count,mce_type,"
    "moments_count,on_public_profile,orientation,play_as,ready_to_edit,ready_to_view,"
    "resolution,source_duration,token,type,width,submitted_at,thumbnail_available,"
    "captured_at_timezone,available_labels"
)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class MediaItem:
    id: str
    type: str
    filename: str
    captured_at: str
    captured_at_timezone: str | None
    file_size: int | None
    item_count: int | None
    camera_model: str | None
    mce_type: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> MediaItem:
        return cls(
            id=str(data.get("id", "")),
            type=str(data.get("type") or ""),
            filename=str(data.get("filename") or ""),
            captured_at=str(data.get("captured_at") or data.get("created_at") or ""),
            captured_at_timezone=data.get("captured_at_timezone"),
            file_size=_int(data.get("file_size")),
            item_count=_int(data.get("item_count")),
            camera_model=data.get("camera_model"),
            mce_type=data.get("mce_type"),
            raw=data,
        )

    @property
    def raw_json(self) -> str:
        return json.dumps(self.raw, separators=(",", ":"))

    def skip_reason(self) -> str | None:
        """Why this item should never be downloaded, or None."""
        if not self.id:
            return "missing_id"
        if self.type in EXCLUDED_TYPES or self.mce_type:
            return "gopro_generated_edit"
        if self.filename.lower().endswith(".json"):
            return "sidecar_json"
        if not self.captured_at:
            return "missing_captured_at"
        return None


@dataclass(frozen=True)
class SourceFile:
    """One physical file to fetch: a whole media item, or one chapter of it."""

    item_number: int
    filename: str
    url: str
    size: int | None
    checksum: str | None = None
    checksum_algo: str | None = None


def _checksum_of(data: dict[str, Any]) -> tuple[str | None, str | None]:
    for key, algo in (("md5", "md5"), ("sha1", "sha1"), ("etag", "etag")):
        value = data.get(key)
        if value:
            return str(value), algo
    return None, None


# Source URLs look like  .../<media>/source/default/2.mp4  -- the trailing number
# is the chapter index of a long recording.
_CHAPTER_IN_URL = re.compile(r"/source/[^/]+/(\d+)\.[A-Za-z0-9]+(?:\?|$)")

# GoPro camera naming: GX<chapter:02d><file:04d>.MP4, e.g. GX010450 -> GX020450
# is chapter 2 of recording 0450.
_GOPRO_NAME = re.compile(r"^(G[A-Z])(\d{2})(\d{4})(\.[A-Za-z0-9]+)$")


def chapter_number_from_url(url: str, fallback: int) -> int:
    match = _CHAPTER_IN_URL.search(url)
    return int(match.group(1)) if match else fallback


def chapter_filename(base: str, item_number: int) -> str:
    """Name chapter N of a recording the way the camera would have."""
    if item_number <= 1:
        return base
    match = _GOPRO_NAME.match(base)
    if match:
        prefix, _chapter, number, ext = match.groups()
        return f"{prefix}{item_number:02d}{number}{ext}"
    stem, _, ext = base.rpartition(".")
    if not stem:
        return f"{base}_p{item_number:02d}"
    return f"{stem}_p{item_number:02d}.{ext}"


def parse_download_response(
    data: dict[str, Any], item: MediaItem
) -> tuple[list[SourceFile], str | None]:
    """Extract the original-quality file(s) from /media/{id}/download.

    Only `_embedded.variations` entries labelled "source" are ever used.

    `_embedded.files` is deliberately ignored: for videos it points at the
    `high_res_proxy_mp4` transcode (roughly half the bytes of the original),
    while for photos it happens to point at the source. Trusting it silently
    downgrades every video, which is the exact failure this tool exists to
    avoid.

    A long recording is split into chapters that all carry the label "source"
    and are told apart only by the trailing number in their URL path, so every
    matching variation is returned -- not just the first.
    """
    embedded = data.get("_embedded") or {}
    sources = [
        v
        for v in (embedded.get("variations") or [])
        if str(v.get("label", "")).lower() == "source" and v.get("url")
    ]
    if not sources:
        return [], "no_source_variation"

    files: list[SourceFile] = []
    for index, variation in enumerate(sources, start=1):
        number = chapter_number_from_url(str(variation["url"]), index)
        checksum, algo = _checksum_of(variation)
        files.append(
            SourceFile(
                item_number=number,
                filename=chapter_filename(
                    item.filename or f"{item.id}.{variation.get('type', 'bin')}", number
                ),
                url=str(variation["url"]),
                # Variations carry no size. The listing's file_size is the sum
                # across chapters, so it is only a per-file expectation when
                # there is exactly one chapter; otherwise the real size is
                # learned from the response headers at download time.
                size=item.file_size if len(sources) == 1 else None,
                checksum=checksum,
                checksum_algo=algo,
            )
        )
    files.sort(key=lambda f: f.item_number)
    return files, None
