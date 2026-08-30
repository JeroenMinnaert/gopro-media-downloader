"""Byte-level date reading and repair for JPEG and MP4 containers.

The fixtures here mirror the shapes actually seen in a real GoPro Plus library:
photos whose Exif block carries orientation and pixel dimensions but no date at
all (the common case), photos that already have a correct DateTimeOriginal, and
videos with plausible mvhd/tkhd/mdhd creation times.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime

import pytest

from gopro_dl.mediadates import (
    MalformedMedia,
    UnsupportedMedia,
    apply_dates,
    kind_for,
    read_dates,
    scan_jpeg,
)

LOCAL = datetime(2024, 1, 27, 14, 33, 9)
UTC_AT = datetime(2024, 1, 27, 13, 33, 9, tzinfo=UTC)


# -- fixture builders ------------------------------------------------------


def _ifd(entries: list[tuple[int, int, int, bytes]], endian: str, value_at: int):
    """Pack (tag, type, count, payload) entries; long payloads go after."""
    out = bytearray(struct.pack(endian + "H", len(entries)))
    values = bytearray()
    for tag, typ, count, payload in entries:
        if len(payload) <= 4:
            inline = payload.ljust(4, b"\x00")
        else:
            inline = struct.pack(endian + "I", value_at + len(values))
            values += payload
            if len(values) % 2:
                values += b"\x00"
        out += struct.pack(endian + "HHI", tag, typ, count) + inline
    out += struct.pack(endian + "I", 0)
    return bytes(out), bytes(values)


def make_jpeg(
    *,
    dates: dict[int, bytes] | None = None,
    exif_extra: list[tuple[int, int, int, bytes]] | None = None,
    endian: str = ">",
    with_app1: bool = True,
    with_app0: bool = True,
) -> bytes:
    """A minimal but structurally real JPEG.

    `dates` maps Exif-IFD tag -> 20-byte ASCII value; leave it empty to get the
    shape most downloaded photos actually have: an Exif block with no date.
    """
    body = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + b"\x7f" * 64 + b"\xff\xd9"
    head = b"\xff\xd8"
    if with_app0:
        head += b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00H\x00H\x00\x00"
    if not with_app1:
        return head + body

    exif_entries = [(0xA002, 4, 1, struct.pack(endian + "I", 4032))]
    exif_entries += exif_extra or []
    for tag, value in (dates or {}).items():
        exif_entries.append((tag, 2, 20, value))
    exif_entries.sort(key=lambda e: e[0])

    # IFD0 holds orientation plus the pointer to the Exif IFD.
    ifd0_len = 2 + 2 * 12 + 4
    exif_at = 8 + ifd0_len
    exif_len = 2 + len(exif_entries) * 12 + 4
    ifd0_bytes, ifd0_vals = _ifd(
        [
            (0x0112, 3, 1, struct.pack(endian + "H", 1)),
            (0x8769, 4, 1, struct.pack(endian + "I", exif_at)),
        ],
        endian,
        exif_at + exif_len,
    )
    exif_bytes, exif_vals = _ifd(exif_entries, endian, exif_at + exif_len + len(ifd0_vals))
    tiff = (
        (b"MM" if endian == ">" else b"II")
        + struct.pack(endian + "HI", 42, 8)
        + ifd0_bytes
        + exif_bytes
        + ifd0_vals
        + exif_vals
    )
    payload = b"Exif\x00\x00" + tiff
    app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    return head + app1 + body


def _box(btype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + btype + payload


def _fullbox_time(when: datetime, version: int = 0) -> bytes:
    epoch = datetime(1904, 1, 1, tzinfo=UTC)
    seconds = int((when.replace(tzinfo=UTC) - epoch).total_seconds())
    head = bytes([version, 0, 0, 0])
    if version == 1:
        return head + struct.pack(">QQ", seconds, seconds) + b"\x00" * 20
    return head + struct.pack(">II", seconds, seconds) + b"\x00" * 12


def make_mp4(when: datetime, version: int = 0) -> bytes:
    mdia = _box(b"mdia", _box(b"mdhd", _fullbox_time(when, version)))
    trak = _box(b"trak", _box(b"tkhd", _fullbox_time(when, version)) + mdia)
    moov = _box(b"moov", _box(b"mvhd", _fullbox_time(when, version)) + trak)
    return _box(b"ftyp", b"isom" + b"\x00" * 8) + moov + _box(b"mdat", b"\x00" * 32)


def _exif_date(value: datetime) -> bytes:
    return value.strftime("%Y:%m:%d %H:%M:%S").encode() + b"\x00"


# -- reading ---------------------------------------------------------------


def test_photo_with_an_exif_block_but_no_date_reports_it_missing():
    # The common real-world case: apps fall back to the file's mtime because
    # there is no DateTimeOriginal to read.
    dates = scan_jpeg(make_jpeg())
    assert dates.fields == []
    assert "DateTimeOriginal" in dates.missing
    assert dates.can_add


def test_photo_with_no_exif_segment_at_all_can_still_be_given_one():
    dates = scan_jpeg(make_jpeg(with_app1=False))
    assert dates.can_add
    assert dates.exif is not None and dates.exif.seg_start is None


def test_existing_dates_are_read_back():
    raw = make_jpeg(dates={0x9003: _exif_date(LOCAL), 0x9004: _exif_date(LOCAL)})
    dates = scan_jpeg(raw)
    assert {f.name: f.current for f in dates.fields} == {
        "DateTimeOriginal": LOCAL,
        "DateTimeDigitized": LOCAL,
    }
    assert dates.primary == LOCAL


def test_missing_only_the_cosmetic_datetime_is_not_worth_a_rebuild():
    # DateTimeOriginal is what libraries sort by; IFD0 DateTime is a file-change
    # date. Rebuilding for it alone would invalidate the checksum for nothing.
    dates = scan_jpeg(make_jpeg(dates={0x9003: _exif_date(LOCAL), 0x9004: _exif_date(LOCAL)}))
    assert dates.missing == ("DateTime",)
    assert not dates.can_add


def test_a_makernote_blocks_rebuilding():
    raw = make_jpeg(exif_extra=[(0x927C, 7, 8, b"\x01\x02\x03\x04\x05\x06\x07\x08")])
    dates = scan_jpeg(raw)
    assert dates.blocker is not None
    assert not dates.can_add


def test_little_endian_exif_parses():
    raw = make_jpeg(dates={0x9003: _exif_date(LOCAL)}, endian="<")
    assert scan_jpeg(raw).primary == LOCAL


def test_a_non_jpeg_is_rejected_rather_than_guessed_at():
    with pytest.raises(UnsupportedMedia):
        scan_jpeg(b"not a jpeg at all")


def test_kind_for_extensions():
    assert kind_for("a/b.JPG") == "jpeg"
    assert kind_for("a/b.MP4") == "mp4"
    with pytest.raises(UnsupportedMedia):
        kind_for("a/b.gpr")


# -- repairing photos ------------------------------------------------------


def test_adding_dates_preserves_every_image_byte(tmp_path):
    original = make_jpeg()
    path = tmp_path / "IMG_0001.jpg"
    path.write_bytes(original)

    result = apply_dates(path, LOCAL, UTC_AT, read_dates(path))
    assert result.rebuilt
    assert "DateTimeOriginal" in result.added

    after = path.read_bytes()
    assert len(after) == result.size > len(original)
    # Everything from the start-of-scan marker on is the picture itself.
    assert after[after.index(b"\xff\xda") :] == original[original.index(b"\xff\xda") :]

    reread = read_dates(path)
    assert {f.name: f.current for f in reread.fields} == {
        "DateTime": LOCAL,
        "DateTimeOriginal": LOCAL,
        "DateTimeDigitized": LOCAL,
    }


def test_repair_is_idempotent(tmp_path):
    path = tmp_path / "IMG_0002.jpg"
    path.write_bytes(make_jpeg())
    apply_dates(path, LOCAL, UTC_AT, read_dates(path))
    once = path.read_bytes()

    dates = read_dates(path)
    assert not dates.can_add  # nothing left to add
    apply_dates(path, LOCAL, UTC_AT, dates)  # in-place, same values
    assert path.read_bytes() == once


def test_a_photo_without_an_exif_segment_gains_one(tmp_path):
    path = tmp_path / "IMG_0003.jpg"
    path.write_bytes(make_jpeg(with_app1=False))
    apply_dates(path, LOCAL, UTC_AT, read_dates(path))
    assert read_dates(path).primary == LOCAL


def test_a_wrong_date_is_patched_in_place_without_resizing(tmp_path):
    stale = datetime(2019, 5, 4, 1, 2, 3)
    path = tmp_path / "IMG_0004.jpg"
    path.write_bytes(make_jpeg(dates={0x9003: _exif_date(stale), 0x9004: _exif_date(stale)}))
    before = path.stat().st_size

    result = apply_dates(path, LOCAL, UTC_AT, read_dates(path))
    assert not result.rebuilt
    assert path.stat().st_size == before
    assert read_dates(path).primary == LOCAL


def test_rebuilding_a_blocked_photo_is_refused(tmp_path):
    path = tmp_path / "IMG_0005.jpg"
    path.write_bytes(make_jpeg(exif_extra=[(0x927C, 7, 8, b"12345678")]))
    dates = read_dates(path)
    from gopro_dl.mediadates import rebuild_jpeg

    with pytest.raises(MalformedMedia):
        rebuild_jpeg(path.read_bytes(), dates, LOCAL, UTC_AT)


# -- repairing videos ------------------------------------------------------


@pytest.mark.parametrize("version", [0, 1])
def test_video_times_are_read_and_patched_in_place(tmp_path, version):
    shot = datetime(2025, 2, 1, 14, 56, 56)
    path = tmp_path / "GX010500.MP4"
    path.write_bytes(make_mp4(shot, version))

    dates = read_dates(path)
    assert [f.name for f in dates.fields] == ["mvhd", "tkhd", "mdhd"]
    assert dates.primary == shot

    before = path.stat().st_size
    corrected = datetime(2025, 2, 1, 15, 56, 56)
    result = apply_dates(path, corrected, corrected.replace(tzinfo=UTC), dates)

    assert not result.rebuilt  # containers are never rewritten
    assert path.stat().st_size == before
    assert read_dates(path).primary == corrected


def test_a_video_with_no_moov_is_reported_not_guessed(tmp_path):
    path = tmp_path / "broken.mp4"
    path.write_bytes(_box(b"ftyp", b"isom" + b"\x00" * 8) + _box(b"mdat", b"\x00" * 16))
    with pytest.raises(MalformedMedia):
        read_dates(path)
