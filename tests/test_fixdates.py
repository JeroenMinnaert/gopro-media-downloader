"""`fix-dates`: reconciling what a file says about itself with what GoPro says."""

from __future__ import annotations

import os
from datetime import UTC, datetime

from conftest import make_item
from test_mediadates import _exif_date, make_jpeg, make_mp4

from gopro_dl.fixdates import expected_times, fix_dates
from gopro_dl.mediadates import read_dates
from gopro_dl.verify import verify

# 22:30 UTC on the 14th is already the 15th in Brussels -- the same conversion
# that names the folder must be the one written into the Exif.
CAPTURED_AT = "2023-07-14T22:30:00Z"
FOLDER = "2023-07-15"
LOCAL = datetime(2023, 7, 15, 0, 30, 0)


def seed(manifest, dest, filename, payload, *, media_id="aaa111", tz="Europe/Brussels"):
    item = make_item(media_id, filename=filename, captured_at=CAPTURED_AT, captured_at_timezone=tz)
    manifest.upsert_item(item, FOLDER)
    rel = f"{FOLDER}/{filename}"
    path = dest / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    size = len(payload)
    manifest.upsert_file(media_id, 1, filename, rel, size, checksum="abc-2", checksum_algo="s3-etag")
    manifest.mark_done(manifest.get_file(media_id, 1)["id"], size)
    return path


def test_expected_times_match_the_folder_conversion():
    local, utc = expected_times(CAPTURED_AT, "Europe/Brussels")
    assert local == LOCAL
    assert utc == datetime(2023, 7, 14, 22, 30, tzinfo=UTC)


def test_a_photo_with_no_exif_date_gets_one(manifest, tmp_path):
    path = seed(manifest, tmp_path, "IMG_0001.jpg", make_jpeg())

    report = fix_dates(manifest, tmp_path)

    assert len(report.fixed) == 1 and report.added_tags == 1 and not report.failed
    assert read_dates(path).primary == LOCAL


def test_the_manifest_follows_the_file_so_verify_stays_clean(manifest, tmp_path):
    # A repaired photo is deliberately no longer the origin's bytes. If the
    # manifest kept describing the origin, `verify --fix` would delete the
    # repair and download the broken copy again.
    path = seed(manifest, tmp_path, "IMG_0002.jpg", make_jpeg())
    origin_size = manifest.get_file("aaa111", 1)["expected_size"]

    fix_dates(manifest, tmp_path)

    row = manifest.get_file("aaa111", 1)
    assert row["origin_checksum"] == "abc-2"
    assert row["origin_size"] == origin_size
    assert row["checksum_algo"] == "md5"
    assert row["expected_size"] == path.stat().st_size != origin_size
    assert row["dates_fixed_at"]

    report = verify(manifest, tmp_path, deep=True, fix=True)
    assert report.problems == 0 and report.ok == 1
    assert path.exists()


def test_a_later_sync_does_not_undo_the_repair(manifest, tmp_path):
    seed(manifest, tmp_path, "IMG_0003.jpg", make_jpeg())
    fix_dates(manifest, tmp_path)
    repaired = manifest.get_file("aaa111", 1)

    # refresh_manifest re-upserts every file it enumerates, with the origin's
    # size and checksum. Those must not overwrite the repaired file's.
    manifest.upsert_file(
        "aaa111", 1, "IMG_0003.jpg", f"{FOLDER}/IMG_0003.jpg", repaired["origin_size"],
        checksum="abc-2", checksum_algo="s3-etag",
    )

    row = manifest.get_file("aaa111", 1)
    assert row["expected_size"] == repaired["expected_size"]
    assert row["checksum"] == repaired["checksum"]
    assert row["checksum_algo"] == "md5"


def test_a_correct_photo_is_left_byte_for_byte_alone(manifest, tmp_path):
    payload = make_jpeg(dates={0x9003: _exif_date(LOCAL), 0x9004: _exif_date(LOCAL)})
    path = seed(manifest, tmp_path, "IMG_0004.jpg", payload)

    report = fix_dates(manifest, tmp_path)

    assert not report.fixed
    assert path.read_bytes() == payload
    assert manifest.get_file("aaa111", 1)["dates_fixed_at"] is None


def test_a_wrong_photo_date_is_corrected(manifest, tmp_path):
    stale = datetime(2019, 1, 1, 9, 0, 0)
    path = seed(
        manifest, tmp_path, "IMG_0005.jpg",
        make_jpeg(dates={0x9003: _exif_date(stale), 0x9004: _exif_date(stale)}),
    )

    report = fix_dates(manifest, tmp_path)

    assert len(report.fixed) == 1
    assert report.fixed[0][1] == "2019-01-01 09:00:00"
    assert read_dates(path).primary == LOCAL


def test_dry_run_changes_nothing(manifest, tmp_path):
    payload = make_jpeg()
    path = seed(manifest, tmp_path, "IMG_0006.jpg", payload)

    report = fix_dates(manifest, tmp_path, dry_run=True)

    assert len(report.fixed) == 1
    assert path.read_bytes() == payload
    assert manifest.get_file("aaa111", 1)["dates_fixed_at"] is None


def test_the_modification_time_is_pointed_at_the_capture(manifest, tmp_path):
    # This is what a photo library falls back to, and after a download it is
    # the time of the download.
    path = seed(manifest, tmp_path, "IMG_0007.jpg", make_jpeg())
    os.utime(path, (0, 0))

    fix_dates(manifest, tmp_path)

    assert path.stat().st_mtime == datetime(2023, 7, 14, 22, 30, tzinfo=UTC).timestamp()


def test_mtime_can_be_left_alone(manifest, tmp_path):
    path = seed(manifest, tmp_path, "IMG_0008.jpg", make_jpeg())
    os.utime(path, (0, 0))

    fix_dates(manifest, tmp_path, set_mtime=False)

    assert path.stat().st_mtime == 0


def test_a_video_holding_capture_local_time_is_left_alone_by_default(manifest, tmp_path):
    # Cameras routinely write local wall time into the field the spec calls
    # UTC. That is a convention, not damage.
    path = seed(manifest, tmp_path, "GX010001.MP4", make_mp4(LOCAL))
    before = path.read_bytes()

    assert not fix_dates(manifest, tmp_path).fixed
    assert path.read_bytes() == before

    report = fix_dates(manifest, tmp_path, video_utc=True)
    assert len(report.fixed) == 1
    assert read_dates(path).primary == datetime(2023, 7, 14, 22, 30)


def test_a_genuinely_wrong_video_date_is_corrected(manifest, tmp_path):
    path = seed(manifest, tmp_path, "GX010002.MP4", make_mp4(datetime(2015, 3, 3, 3, 3, 3)))
    before = path.stat().st_size

    report = fix_dates(manifest, tmp_path)

    assert len(report.fixed) == 1
    assert path.stat().st_size == before  # never rebuilt
    assert read_dates(path).primary == datetime(2023, 7, 14, 22, 30)


def test_an_unsupported_file_still_gets_its_mtime(manifest, tmp_path):
    path = seed(manifest, tmp_path, "GOPR0001.GPR", b"raw bytes")
    os.utime(path, (0, 0))

    report = fix_dates(manifest, tmp_path)

    assert report.mtime_only == 1 and not report.failed
    assert path.stat().st_mtime == datetime(2023, 7, 14, 22, 30, tzinfo=UTC).timestamp()


def test_a_missing_file_is_reported_not_fatal(manifest, tmp_path):
    path = seed(manifest, tmp_path, "IMG_0009.jpg", make_jpeg())
    path.unlink()

    report = fix_dates(manifest, tmp_path)

    assert report.skipped == [(f"{FOLDER}/IMG_0009.jpg", "file is not on disk")]
    assert not report.failed


def test_since_and_until_bound_the_work(manifest, tmp_path):
    seed(manifest, tmp_path, "IMG_0010.jpg", make_jpeg())

    assert fix_dates(manifest, tmp_path, since="2024-01-01", dry_run=True).checked == 0
    assert fix_dates(manifest, tmp_path, until="2020-01-01", dry_run=True).checked == 0
    assert fix_dates(manifest, tmp_path, since=FOLDER, until=FOLDER, dry_run=True).checked == 1


def test_the_fallback_timezone_is_the_one_the_sync_used(manifest, tmp_path):
    from gopro_dl.paths import parse_timezone

    path = seed(manifest, tmp_path, "IMG_0011.jpg", make_jpeg(), tz=None)
    fix_dates(manifest, tmp_path, fallback_timezone=parse_timezone("Europe/Brussels"))
    assert read_dates(path).primary == LOCAL
