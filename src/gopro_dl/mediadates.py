"""Read and repair the capture dates embedded in JPEG and MP4 files.

GoPro's API is the authority on when a clip was shot (`captured_at`, plus the
`captured_at_timezone` that decides which local day it belongs to -- the same
pair that names the YYYY-MM-DD folders). What is stored *inside* the files does
not always agree, and the two container families fail differently:

* **Photos** frequently carry an Exif block with no date tag at all -- just
  orientation and pixel dimensions. Nothing is "wrong" to correct; the tags are
  missing, so photo libraries fall back to the file's modification time, which
  for a downloaded file is when it was downloaded. Fixing these means *adding*
  DateTimeOriginal/DateTimeDigitized/DateTime, which resizes the Exif segment,
  so the JPEG is rebuilt: prefix + new APP1 + the original bytes after it,
  written to a temp file and atomically renamed.
* **Videos** normally have plausible `mvhd`/`tkhd`/`mdhd` creation times. Those
  are fixed-width integers, so a wrong one is corrected by overwriting a few
  bytes in place. We never rebuild an ISO-BMFF container: moving boxes around a
  multi-gigabyte file to correct 8 bytes is all risk and no benefit.

Two clocks are in play and they are not interchangeable:

* Exif `DateTimeOriginal` / `DateTime` / `DateTimeDigitized` are *local wall
  time* with no zone attached. We write capture-local time, which is what makes
  the Exif agree with the folder the file lives in.
* Exif GPS `GPSDateStamp` / `GPSTimeStamp` and the ISO-BMFF creation times are
  UTC by spec, so those get UTC.

MP4 parsing walks the box tree with seeks rather than slurping a header: a
destination is usually a NAS, and reading even 4 MB from each of a thousand
videos is gigabytes over SMB to inspect a few hundred bytes.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial

from .paths import split_ext

# ISO-BMFF timestamps count seconds from 1904-01-01T00:00:00Z.
_QT_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)
_QT_MAX = 2**64 - 1

# Exif ASCII dates are "YYYY:MM:DD HH:MM:SS\0" -- always 20 bytes.
_EXIF_DT_LEN = 20

_JPEG_EXTS = {".jpg", ".jpeg", ".thm"}
_MP4_EXTS = {".mp4", ".mov", ".m4v", ".lrv"}

# A JPEG's Exif lives in one APP1 segment, whose payload is 16-bit-length bound.
_MAX_APP1_PAYLOAD = 0xFFFF - 2
# Enough to cover APP0/APP1/ICC/XMP before the image data starts.
_JPEG_HEAD = 1024 * 1024

_TAG_DATETIME = 0x0132  # IFD0 DateTime
_TAG_MAKERNOTE = 0x927C
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_DATETIME_DIGITIZED = 0x9004
_TAG_EXIF_IFD = 0x8769
_TAG_GPS_IFD = 0x8825
_TAG_GPS_DATESTAMP = 0x001D  # ASCII "YYYY:MM:DD\0", UTC
_TAG_GPS_TIMESTAMP = 0x0007  # 3 rationals h/m/s, UTC

_LOCAL_ASCII_TAGS = {
    _TAG_DATETIME: "DateTime",
    _TAG_DATETIME_ORIGINAL: "DateTimeOriginal",
    _TAG_DATETIME_DIGITIZED: "DateTimeDigitized",
}
# The tags a complete photo carries, named once so nothing drifts.
_DATE_TAG_NAMES = tuple(_LOCAL_ASCII_TAGS.values())
# Where each date tag belongs when we have to create it.
_IFD0_DATE_TAGS = (_TAG_DATETIME,)
_EXIF_DATE_TAGS = (_TAG_DATETIME_ORIGINAL, _TAG_DATETIME_DIGITIZED)

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


class UnsupportedMedia(Exception):
    """The file is not a container whose dates we know how to touch."""


class MalformedMedia(Exception):
    """The container is the right kind but its structure did not parse."""


# -- what we found ---------------------------------------------------------


@dataclass
class DateField:
    """One rewritable date, located by absolute byte offset in the file."""

    name: str
    offset: int
    length: int
    clock: str  # "local" or "utc"
    current: datetime | None
    # Renders a replacement for this field. Returns None when the value cannot
    # be expressed in the bytes available, which only the variable-length
    # QuickTime day atom can do.
    encoder: Callable[[datetime, int], bytes | None]
    # ISO-BMFF calls these fields UTC, but cameras routinely write capture-local
    # time into them. A value matching either reading is not evidence of damage.
    ambiguous_clock: bool = False


@dataclass
class MediaDates:
    kind: str  # "jpeg" or "mp4"
    fields: list[DateField] = field(default_factory=list)
    missing: tuple[str, ...] = ()  # date tags a JPEG has no slot for
    blocker: str | None = None  # why this JPEG must not be rebuilt
    exif: JpegExif | None = None

    @property
    def primary(self) -> datetime | None:
        """The timestamp a photo library would most likely show."""
        for want in ("DateTimeOriginal", "DateTime", "mvhd", "tkhd", "mdhd"):
            for f in self.fields:
                if f.name == want and f.current is not None:
                    return f.current
        for f in self.fields:
            if f.current is not None:
                return f.current
        return None

    @property
    def can_add(self) -> bool:
        """Is this a photo worth rebuilding to give it a capture date?

        Only a missing DateTimeOriginal justifies a rebuild. IFD0's DateTime is
        a file-change date that nothing sorts by, so a photo that has the real
        capture tag and merely lacks that one is left byte-for-byte alone --
        rebuilding it would invalidate its origin checksum to no end.
        """
        return (
            self.kind == "jpeg"
            and "DateTimeOriginal" in self.missing
            and self.blocker is None
        )


# -- encoding --------------------------------------------------------------


def _exif_datetime(value: datetime, length: int = _EXIF_DT_LEN) -> bytes:
    return value.strftime("%Y:%m:%d %H:%M:%S").encode("ascii") + b"\x00"


def _exif_datestamp(value: datetime, length: int = 11) -> bytes:
    return value.strftime("%Y:%m:%d").encode("ascii") + b"\x00"


def _gps_timestamp(endian: str, value: datetime, length: int = 24) -> bytes:
    return struct.pack(endian + "6I", value.hour, 1, value.minute, 1, value.second, 1)


def _qt_time(value: datetime, length: int) -> bytes:
    seconds = int((value.replace(tzinfo=UTC) - _QT_EPOCH).total_seconds())
    if seconds < 0 or seconds > _QT_MAX:
        raise ValueError(f"{value} is outside the ISO-BMFF epoch")
    return struct.pack(">I" if length == 4 else ">Q", seconds)


def _qt_day(value: datetime, length: int) -> bytes | None:
    """©day is variable length, so only rewrite when the new text fits exactly."""
    for text in (
        value.strftime("%Y-%m-%dT%H:%M:%SZ"),
        value.strftime("%Y-%m-%dT%H:%M:%S+0000"),
        value.strftime("%Y-%m-%d"),
    ):
        raw = text.encode("ascii")
        if len(raw) == length:
            return raw
        if len(raw) < length:
            return raw.ljust(length, b"\x00")
    return None


def _parse_exif_datetime(raw: bytes) -> datetime | None:
    text = raw.split(b"\x00", 1)[0].decode("ascii", "replace").strip()
    if not text or text.startswith("0000"):
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# -- JPEG: segment location ------------------------------------------------


def _iter_jpeg_segments(data: bytes):
    """Yield (marker, seg_start, seg_end) for the segments before the scan."""
    if data[:2] != b"\xff\xd8":
        raise UnsupportedMedia("not a JPEG (no SOI marker)")
    pos = 2
    end = len(data)
    while pos + 4 <= end:
        if data[pos] != 0xFF:
            raise MalformedMedia(f"expected a marker at byte {pos}")
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker in (0xDA, 0xD9):  # start of scan / end of image
            return
        (seglen,) = struct.unpack_from(">H", data, pos + 2)
        if seglen < 2 or pos + 2 + seglen > end:
            raise MalformedMedia("segment length runs past the end of the file")
        yield marker, pos, pos + 2 + seglen
        pos += 2 + seglen


def _locate_app1(data: bytes) -> tuple[int, int] | None:
    """(segment start, segment end) of the Exif APP1 segment, if there is one."""
    for marker, start, end in _iter_jpeg_segments(data):
        if marker == 0xE1 and data[start + 4 : start + 10] == b"Exif\x00\x00":
            return start, end
    return None


def _insertion_point(data: bytes) -> int:
    """Where a new APP1 goes: after APP0 if present, otherwise right after SOI."""
    first = next(_iter_jpeg_segments(data), None)
    if first is None:
        return 2
    marker, start, end = first
    return end if marker == 0xE0 else start


# -- JPEG: TIFF structure --------------------------------------------------


@dataclass
class Entry:
    tag: int
    typ: int
    count: int
    value: bytes  # the payload itself, inline or gathered from its offset


@dataclass
class JpegExif:
    """A JPEG's Exif block, parsed far enough to be rebuilt losslessly."""

    endian: str
    ifd0: list[Entry]
    exif: list[Entry]
    gps: list[Entry]
    seg_start: int | None  # None when the file has no APP1 at all
    seg_end: int | None


def _payload_size(entry_typ: int, count: int) -> int:
    size = _TYPE_SIZE.get(entry_typ)
    if size is None:
        raise MalformedMedia(f"unknown TIFF type {entry_typ}")
    return size * count


def _read_ifd(data: bytes, tiff: int, endian: str, offset: int) -> tuple[list[Entry], int]:
    start = tiff + offset
    if start + 2 > len(data):
        raise MalformedMedia("IFD offset past the end of the file")
    (count,) = struct.unpack_from(endian + "H", data, start)
    entries: list[Entry] = []
    for i in range(count):
        at = start + 2 + i * 12
        if at + 12 > len(data):
            raise MalformedMedia("IFD entry past the end of the file")
        tag, typ, n, raw = struct.unpack_from(endian + "HHII", data, at)
        size = _payload_size(typ, n)
        if size <= 4:
            value = data[at + 8 : at + 8 + size]
        else:
            begin = tiff + raw
            if begin + size > len(data):
                raise MalformedMedia(f"value of tag 0x{tag:04X} past the end of the file")
            value = data[begin : begin + size]
        entries.append(Entry(tag, typ, n, value))
    nxt = start + 2 + count * 12
    if nxt + 4 > len(data):
        raise MalformedMedia("IFD is missing its next-IFD pointer")
    (next_offset,) = struct.unpack_from(endian + "I", data, nxt)
    return entries, next_offset


def _value_offset(data: bytes, tiff: int, endian: str, ifd_offset: int, tag: int) -> int | None:
    """Absolute file offset of a tag's out-of-line value, for in-place patching."""
    start = tiff + ifd_offset
    (count,) = struct.unpack_from(endian + "H", data, start)
    for i in range(count):
        at = start + 2 + i * 12
        etag, typ, n, raw = struct.unpack_from(endian + "HHII", data, at)
        if etag == tag:
            return tiff + raw if _payload_size(typ, n) > 4 else at + 8
    return None


def scan_jpeg(data: bytes) -> MediaDates:
    """Parse a JPEG's Exif dates, and note which ones are missing."""
    located = _locate_app1(data)
    if located is None:
        return MediaDates(
            "jpeg",
            missing=_DATE_TAG_NAMES,
            exif=JpegExif(">", [], [], [], None, None),
        )

    seg_start, seg_end = located
    tiff = seg_start + 10
    order = data[tiff : tiff + 2]
    if order == b"II":
        endian = "<"
    elif order == b"MM":
        endian = ">"
    else:
        raise MalformedMedia("unknown TIFF byte order")
    magic, ifd0_off = struct.unpack_from(endian + "HI", data, tiff + 2)
    if magic != 42:
        raise MalformedMedia("bad TIFF magic")

    ifd0, next_ifd = _read_ifd(data, tiff, endian, ifd0_off)
    by_tag = {e.tag: e for e in ifd0}

    def sub(tag: int) -> tuple[list[Entry], int | None]:
        entry = by_tag.get(tag)
        if entry is None or entry.typ != 4 or entry.count != 1:
            return [], None
        (off,) = struct.unpack(endian + "I", entry.value)
        return _read_ifd(data, tiff, endian, off)[0], off

    exif_entries, exif_off = sub(_TAG_EXIF_IFD)
    gps_entries, gps_off = sub(_TAG_GPS_IFD)

    fields: list[DateField] = []

    def add_ascii(entries, ifd_off, tag, name, length, clock, encoder):
        entry = next((e for e in entries if e.tag == tag), None)
        if entry is None or entry.typ != 2 or entry.count != length:
            return False
        at = _value_offset(data, tiff, endian, ifd_off, tag)
        if at is None:
            return False
        fields.append(
            DateField(name, at, length, clock, _parse_exif_datetime(entry.value), encoder)
        )
        return True

    for entries, offset, tags in (
        (ifd0, ifd0_off, _IFD0_DATE_TAGS),
        (exif_entries, exif_off, _EXIF_DATE_TAGS),
    ):
        if offset is None:
            continue
        for tag in tags:
            add_ascii(
                entries, offset, tag, _LOCAL_ASCII_TAGS[tag], _EXIF_DT_LEN, "local", _exif_datetime
            )
    if gps_off is not None:
        add_ascii(
            gps_entries, gps_off, _TAG_GPS_DATESTAMP, "GPSDateStamp", 11, "utc", _exif_datestamp
        )
        stamp = next((e for e in gps_entries if e.tag == _TAG_GPS_TIMESTAMP), None)
        if stamp is not None and stamp.typ == 5 and stamp.count == 3:
            at = _value_offset(data, tiff, endian, gps_off, _TAG_GPS_TIMESTAMP)
            if at is not None:
                fields.append(
                    DateField(
                        "GPSTimeStamp",
                        at,
                        24,
                        "utc",
                        None,  # only meaningful next to GPSDateStamp
                        partial(_gps_timestamp, endian),
                    )
                )

    found = {f.name for f in fields}
    missing = tuple(name for name in _DATE_TAG_NAMES if name not in found)

    # Rebuilding relocates every value, which silently breaks anything holding
    # TIFF-absolute offsets of its own. Rather than guess, refuse those files.
    blocker = None
    if any(e.tag == _TAG_MAKERNOTE for e in exif_entries):
        blocker = "Exif contains a MakerNote, which a rebuild would invalidate"
    elif next_ifd:
        blocker = "Exif has a thumbnail IFD, which a rebuild would invalidate"

    return MediaDates(
        "jpeg",
        fields=fields,
        missing=missing,
        blocker=blocker,
        exif=JpegExif(endian, ifd0, exif_entries, gps_entries, seg_start, seg_end),
    )


# -- JPEG: rebuilding ------------------------------------------------------


def _ifd_size(n: int) -> int:
    return 2 + n * 12 + 4


def _serialise_ifd(entries: list[Entry], endian: str, value_at: int) -> tuple[bytes, bytes, int]:
    """Pack one IFD; long values go to a shared area starting at `value_at`."""
    entries = sorted(entries, key=lambda e: e.tag)
    out = bytearray(struct.pack(endian + "H", len(entries)))
    values = bytearray()
    for e in entries:
        size = _payload_size(e.typ, e.count)
        if size <= 4:
            payload = e.value.ljust(4, b"\x00")[:4]
        else:
            payload = struct.pack(endian + "I", value_at + len(values))
            values += e.value
            if len(values) % 2:  # TIFF offsets are word aligned
                values += b"\x00"
        out += struct.pack(endian + "HHI", e.tag, e.typ, e.count) + payload
    out += struct.pack(endian + "I", 0)  # no next IFD
    return bytes(out), bytes(values), value_at + len(values)


def build_tiff(exif: JpegExif) -> bytes:
    """Re-serialise a parsed Exif block into a fresh, self-consistent TIFF."""
    endian = exif.endian
    ifd0 = [e for e in exif.ifd0 if e.tag not in (_TAG_EXIF_IFD, _TAG_GPS_IFD)]

    off_ifd0 = 8
    off_exif = off_ifd0 + _ifd_size(len(ifd0) + bool(exif.exif) + bool(exif.gps))
    off_gps = off_exif + (_ifd_size(len(exif.exif)) if exif.exif else 0)
    values_at = off_gps + (_ifd_size(len(exif.gps)) if exif.gps else 0)

    if exif.exif:
        ifd0.append(Entry(_TAG_EXIF_IFD, 4, 1, struct.pack(endian + "I", off_exif)))
    if exif.gps:
        ifd0.append(Entry(_TAG_GPS_IFD, 4, 1, struct.pack(endian + "I", off_gps)))

    ifd0_bytes, v0, values_at = _serialise_ifd(ifd0, endian, values_at)
    exif_bytes, v1, values_at = (
        _serialise_ifd(exif.exif, endian, values_at) if exif.exif else (b"", b"", values_at)
    )
    gps_bytes, v2, _ = (
        _serialise_ifd(exif.gps, endian, values_at) if exif.gps else (b"", b"", values_at)
    )

    header = (b"II" if endian == "<" else b"MM") + struct.pack(endian + "HI", 42, off_ifd0)
    return header + ifd0_bytes + exif_bytes + gps_bytes + v0 + v1 + v2


def _ascii_entry(tag: int, value: datetime) -> Entry:
    return Entry(tag, 2, _EXIF_DT_LEN, _exif_datetime(value))


def rebuild_jpeg(data: bytes, dates: MediaDates, local: datetime, utc: datetime) -> bytes:
    """Return the JPEG with a complete set of Exif date tags.

    Only the Exif segment is touched: everything before it and every byte after
    it -- including the entire compressed image -- is copied through unchanged.
    """
    exif = dates.exif
    if exif is None:
        raise MalformedMedia("no Exif structure to rebuild")
    if dates.blocker:
        raise MalformedMedia(dates.blocker)

    def put(entries: list[Entry], tag: int, value: datetime) -> None:
        new = _ascii_entry(tag, value)
        for i, e in enumerate(entries):
            if e.tag == tag:
                entries[i] = new
                return
        entries.append(new)

    ifd0 = list(exif.ifd0)
    exif_ifd = list(exif.exif)
    gps = list(exif.gps)
    put(ifd0, _TAG_DATETIME, local)
    put(exif_ifd, _TAG_DATETIME_ORIGINAL, local)
    put(exif_ifd, _TAG_DATETIME_DIGITIZED, local)
    if any(e.tag == _TAG_GPS_DATESTAMP for e in gps):
        gps = [e for e in gps if e.tag != _TAG_GPS_DATESTAMP]
        gps.append(Entry(_TAG_GPS_DATESTAMP, 2, 11, _exif_datestamp(utc)))

    tiff = build_tiff(JpegExif(exif.endian, ifd0, exif_ifd, gps, None, None))
    payload = b"Exif\x00\x00" + tiff
    if len(payload) + 2 > _MAX_APP1_PAYLOAD:
        raise MalformedMedia("rebuilt Exif segment would exceed the APP1 size limit")
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload

    if exif.seg_start is None:
        at = _insertion_point(data)
        return data[:at] + segment + data[at:]
    return data[: exif.seg_start] + segment + data[exif.seg_end :]


# -- MP4 / ISO-BMFF --------------------------------------------------------


def _iter_boxes(fh, start: int, end: int):
    """Yield (type, payload_start, payload_end) for boxes in [start, end)."""
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        header_bytes = fh.read(8)
        if len(header_bytes) < 8:
            return
        (size,) = struct.unpack_from(">I", header_bytes, 0)
        btype = header_bytes[4:8]
        header = 8
        if size == 1:
            extra = fh.read(8)
            if len(extra) < 8:
                raise MalformedMedia("truncated 64-bit box header")
            (size,) = struct.unpack(">Q", extra)
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise MalformedMedia(f"box {btype!r} at {pos} overruns its parent")
        yield btype, pos + header, pos + size
        pos += size


def _header_box_field(fh, name: str, body: int, limit: int) -> DateField:
    """creation_time out of an mvhd/tkhd/mdhd full box."""
    fh.seek(body)
    head = fh.read(12)
    if len(head) < 12 or body + 4 > limit:
        raise MalformedMedia(f"{name} is truncated")
    version = head[0]
    width = 8 if version == 1 else 4
    at = body + 4
    if at + width > limit:
        raise MalformedMedia(f"{name} creation_time is truncated")
    (seconds,) = struct.unpack_from(">Q" if width == 8 else ">I", head, 4)
    current = None
    if seconds:
        try:
            current = (_QT_EPOCH + timedelta(seconds=seconds)).replace(tzinfo=None)
        except OverflowError:
            current = None
    return DateField(name, at, width, "utc", current, _qt_time, ambiguous_clock=True)


def _day_atom_field(fh, body: int, limit: int) -> DateField | None:
    """QuickTime's ©day atom: an ISO date string we patch only if it fits."""
    if limit - body > 256:
        return None
    fh.seek(body)
    payload = fh.read(limit - body)
    start = 0
    if len(payload) >= 4 and struct.unpack_from(">H", payload, 0)[0] == len(payload) - 4:
        # The standard QuickTime metadata shape: uint16 text length and uint16
        # language code ahead of the text. Its first byte is a NUL for any
        # realistic length, so reading the payload as bare text finds nothing
        # and the field goes silently unrepaired.
        start = 4
    text = payload[start:]
    stripped = text.split(b"\x00", 1)[0].decode("ascii", "replace").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            current = datetime.strptime(stripped, fmt)
        except ValueError:
            continue
        if current.tzinfo is not None:
            current = current.astimezone(UTC).replace(tzinfo=None)
        return DateField(
            "day", body + start, len(text), "utc", current, _qt_day, ambiguous_clock=True
        )
    return None


def _scan_udta(fh, body: int, limit: int) -> list[DateField]:
    out = []
    for btype, dbody, dlimit in _iter_boxes(fh, body, limit):
        if btype == b"\xa9day":
            found = _day_atom_field(fh, dbody, dlimit)
            if found is not None:
                out.append(found)
    return out


def scan_mp4(fh, size: int) -> MediaDates:
    fields: list[DateField] = []
    moov = None
    for btype, body, limit in _iter_boxes(fh, 0, size):
        if btype == b"moov":
            moov = (body, limit)
            break
    if moov is None:
        raise MalformedMedia("no moov box")

    for btype, body, limit in _iter_boxes(fh, *moov):
        if btype == b"mvhd":
            fields.append(_header_box_field(fh, "mvhd", body, limit))
        elif btype == b"udta":
            fields.extend(_scan_udta(fh, body, limit))
        elif btype == b"trak":
            for ttype, tbody, tlimit in _iter_boxes(fh, body, limit):
                if ttype == b"tkhd":
                    fields.append(_header_box_field(fh, "tkhd", tbody, tlimit))
                elif ttype == b"udta":
                    fields.extend(_scan_udta(fh, tbody, tlimit))
                elif ttype == b"mdia":
                    for mtype, mbody, mlimit in _iter_boxes(fh, tbody, tlimit):
                        if mtype == b"mdhd":
                            fields.append(_header_box_field(fh, "mdhd", mbody, mlimit))
    return MediaDates("mp4", fields)


# -- file-level API --------------------------------------------------------


def kind_for(path) -> str:
    ext = split_ext(str(path))[1].lower()
    if ext in _JPEG_EXTS:
        return "jpeg"
    if ext in _MP4_EXTS:
        return "mp4"
    raise UnsupportedMedia(f"no date support for {ext or 'extension-less'} files")


def read_dates(path) -> MediaDates:
    """Parse the date fields of a file on disk (`kind_for` decides how)."""
    kind = kind_for(path)
    if kind == "jpeg":
        with open(path, "rb") as fh:
            return scan_jpeg(fh.read(_JPEG_HEAD))
    with open(path, "rb") as fh:
        return scan_mp4(fh, os.path.getsize(path))


@dataclass
class ApplyResult:
    written: list[str] = field(default_factory=list)
    rebuilt: bool = False
    # md5 of the bytes just written, when a rebuild had the whole file in hand
    # anyway. Saves reading a repaired photo back over the network to hash it.
    digest: str | None = None


def apply_dates(path, local: datetime, utc: datetime, dates: MediaDates) -> ApplyResult:
    """Write `local`/`utc` into the file, in place or by rebuilding its Exif."""
    local = local.replace(tzinfo=None)
    utc = utc.replace(tzinfo=None)

    if dates.can_add:
        return _rebuild_in_place(path, local, utc, dates)

    writes: list[tuple[int, bytes, str]] = []
    for f in dates.fields:
        value = local if f.clock == "local" else utc
        raw = f.encoder(value, f.length)
        if raw is None or len(raw) != f.length:
            continue
        writes.append((f.offset, raw, f.name))
    if not writes:
        return ApplyResult()
    with open(path, "r+b") as fh:
        for offset, raw, _ in writes:
            fh.seek(offset)
            fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    return ApplyResult(written=[name for _, _, name in writes])


def _rebuild_in_place(path, local: datetime, utc: datetime, dates: MediaDates) -> ApplyResult:
    """Rewrite a JPEG through a temp file so a crash can never truncate it."""
    with open(path, "rb") as fh:
        original = fh.read()
    rebuilt = rebuild_jpeg(original, dates, local, utc)

    # The rebuilt Exif must read back as what we meant to write, and the image
    # itself must be untouched -- a rebuild that fails either is not written.
    check = scan_jpeg(rebuilt)
    got = {f.name: f.current for f in check.fields}
    for name in _DATE_TAG_NAMES:
        if got.get(name) != local:
            raise MalformedMedia(f"rebuilt Exif did not read back {name} correctly")
    if _image_body(rebuilt) != _image_body(original):
        raise MalformedMedia("rebuild would alter the image data")

    tmp = f"{path}.exiftmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(rebuilt)
            fh.flush()
            os.fsync(fh.fileno())
        # A rebuild is a new file, so it would otherwise arrive stamped "now".
        # Carry the original's mode and times over; the caller decides
        # separately whether the mtime should become the capture time.
        shutil.copystat(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return ApplyResult(written=sorted(got), rebuilt=True, digest=hashlib.md5(rebuilt).hexdigest())


def _image_body(data: bytes) -> bytes:
    """Everything from the start-of-scan marker on -- the pixels themselves."""
    pos = 2
    for _marker, _start, end in _iter_jpeg_segments(data):
        pos = end
    return data[pos:]
