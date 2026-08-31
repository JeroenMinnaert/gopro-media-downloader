"""`fix-dates`: reconciling what a file says about itself with what GoPro says."""

from __future__ import annotations

import os
from datetime import UTC, datetime

from conftest import make_item
from test_mediadates import _exif_date, make_jpeg, make_mp4

from gopro_dl.fixdates import expected_times, fix_dates
from gopro_dl.mediadates import apply_dates, read_dates
from gopro_dl.verify import verify

# 22:30 UTC on the 14th is already the 15th in Paris -- the same conversion
# that names the folder must be the one written into the Exif.
CAPTURED_AT = "2023-07-14T22:30:00Z"
FOLDER = "2023-07-15"
LOCAL = datetime(2023, 7, 15, 0, 30, 0)


def seed(manifest, dest, filename, payload, *, media_id="aaa111", tz="Europe/Paris"):
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
    local, utc = expected_times(CAPTURED_AT, "Europe/Paris")
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
    fix_dates(manifest, tmp_path, fallback_timezone=parse_timezone("Europe/Paris"))
    assert read_dates(path).primary == LOCAL


def _seed_clip(manifest, dest, name, embedded, media_id, captured_at=CAPTURED_AT):
    item = make_item(
        media_id, filename=name, captured_at=captured_at, captured_at_timezone="Europe/Paris"
    )
    manifest.upsert_item(item, FOLDER)
    rel = f"{FOLDER}/{name}"
    path = dest / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(make_mp4(embedded))
    manifest.upsert_file(media_id, 1, name, rel, path.stat().st_size)
    manifest.mark_done(manifest.get_file(media_id, 1)["id"], path.stat().st_size)
    return path


def test_a_reset_camera_clock_slides_the_folder_instead_of_flattening_it(manifest, tmp_path):
    # A GoPro that lost power dates a whole morning 2015-01-01, but the clips'
    # relative times are still right. The API knows the date and reports one
    # timestamp for all of them, so writing it verbatim would collapse the
    # ordering.
    clips = [
        ("GX010001.MP4", datetime(2015, 1, 1, 2, 43, 0)),
        ("GX010002.MP4", datetime(2015, 1, 1, 3, 5, 0)),
        ("GX010003.MP4", datetime(2015, 1, 1, 4, 13, 0)),
    ]
    paths = [
        _seed_clip(manifest, tmp_path, name, when, f"clip{n}")
        for n, (name, when) in enumerate(clips)
    ]

    report = fix_dates(manifest, tmp_path)

    assert report.shifted == 3 and len(report.fixed) == 3
    now = [read_dates(p).primary for p in paths]
    # The earliest clip lands on GoPro's time...
    assert now[0] == datetime(2023, 7, 14, 22, 30)
    # ...and the gaps between clips are exactly what they were.
    assert now[1] - now[0] == datetime(2015, 1, 1, 3, 5) - datetime(2015, 1, 1, 2, 43)
    assert now[2] - now[1] == datetime(2015, 1, 1, 4, 13) - datetime(2015, 1, 1, 3, 5)
    assert len(set(now)) == 3


def test_flattening_can_be_asked_for_explicitly(manifest, tmp_path):
    paths = [
        _seed_clip(manifest, tmp_path, f"GX01000{n}.MP4", datetime(2015, 1, 1, 2, 40 + n), f"c{n}")
        for n in range(3)
    ]

    report = fix_dates(manifest, tmp_path, preserve_spacing=False)

    assert report.shifted == 0
    assert {read_dates(p).primary for p in paths} == {datetime(2023, 7, 14, 22, 30)}


def test_a_batch_that_already_shares_one_timestamp_has_no_spacing_to_keep(manifest, tmp_path):
    # The other real shape: every clip carries the same stale value, so there
    # is no ordering to protect and the API's timestamp is written as-is.
    same = datetime(2015, 1, 1, 2, 43, 0)
    paths = [
        _seed_clip(manifest, tmp_path, f"GX01000{n}.MP4", same, f"d{n}") for n in range(3)
    ]

    report = fix_dates(manifest, tmp_path)

    assert report.shifted == 0
    assert {read_dates(p).primary for p in paths} == {datetime(2023, 7, 14, 22, 30)}


def test_a_lone_clip_is_not_treated_as_a_group(manifest, tmp_path):
    path = _seed_clip(manifest, tmp_path, "GX010009.MP4", datetime(2015, 1, 1, 2, 43), "solo")

    report = fix_dates(manifest, tmp_path)

    assert report.shifted == 0
    assert read_dates(path).primary == datetime(2023, 7, 14, 22, 30)


def test_a_second_repair_does_not_mistake_our_own_hash_for_the_origin(manifest, tmp_path):
    # Once a file has been repaired, `checksum` holds a local md5. Repairing it
    # again must not file that away as though it were GoPro's.
    path = seed(manifest, tmp_path, "IMG_0012.jpg", make_jpeg())
    fix_dates(manifest, tmp_path)
    after_first = manifest.get_file("aaa111", 1)
    assert after_first["origin_checksum"] == "abc-2"

    # Knock the date back out so the file needs repairing a second time.
    stale = datetime(2019, 1, 1, 9, 0, 0)
    apply_dates(path, stale, stale.replace(tzinfo=UTC), read_dates(path))
    report = fix_dates(manifest, tmp_path)

    assert len(report.fixed) == 1
    row = manifest.get_file("aaa111", 1)
    assert row["origin_checksum"] == "abc-2"
    assert row["origin_size"] == after_first["origin_size"]
    # Restoring the same dates reproduces the same bytes, so the local digest
    # is stable across repairs -- but it is never the origin's.
    assert row["checksum"] == after_first["checksum"] != "abc-2"


def test_an_origin_checksum_that_is_not_an_etag_is_still_preserved(manifest, tmp_path):
    # models.py records an md5 or sha1 when the API supplies one, so keying the
    # hand-off on "was it an s3-etag" would silently drop those.
    seed(manifest, tmp_path, "IMG_0013.jpg", make_jpeg())
    file_id = manifest.get_file("aaa111", 1)["id"]
    manifest.set_checksum(file_id, "d41d8cd98f00b204e9800998ecf8427e", algo="md5")

    fix_dates(manifest, tmp_path)

    assert manifest.get_file("aaa111", 1)["origin_checksum"] == "d41d8cd98f00b204e9800998ecf8427e"


def test_clips_with_their_own_distinct_api_times_are_not_slid_as_one_batch(manifest, tmp_path):
    """The slide exists for a stopped clock, whose signature is GoPro reporting
    *one* timestamp for the whole batch. Two clips with genuinely different
    capture times and unrelated skews are not that: anchoring the group on the
    earliest would leave the later one still wrong, and re-running would move
    it again."""
    first = _seed_clip(
        manifest, tmp_path, "GX010001.MP4", datetime(2023, 7, 15, 10, 0),
        "one", captured_at="2023-07-15T10:05:00Z",
    )
    second = _seed_clip(
        manifest, tmp_path, "GX010002.MP4", datetime(2023, 7, 15, 11, 0),
        "two", captured_at="2023-07-15T11:07:00Z",
    )

    report = fix_dates(manifest, tmp_path)

    assert report.shifted == 0
    assert read_dates(first).primary == datetime(2023, 7, 15, 10, 5)
    assert read_dates(second).primary == datetime(2023, 7, 15, 11, 7)


def _s3_etag(body: bytes) -> str:
    import hashlib

    return f"{hashlib.md5(hashlib.md5(body).digest()).hexdigest()}-1"


def test_a_requeued_repair_goes_back_to_describing_the_origin(manifest, tmp_path):
    """`verify --fix` means "fetch this from GoPro again", so the row has to
    stop describing our repaired copy -- the file about to land is the
    origin's bytes."""
    payload = make_jpeg()
    origin_etag = _s3_etag(payload)
    item = make_item("aaa111", filename="IMG_0020.jpg", captured_at=CAPTURED_AT,
                     captured_at_timezone="Europe/Paris")
    manifest.upsert_item(item, FOLDER)
    rel = f"{FOLDER}/IMG_0020.jpg"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    manifest.upsert_file("aaa111", 1, "IMG_0020.jpg", rel, len(payload), origin_etag, "s3-etag")
    manifest.mark_done(manifest.get_file("aaa111", 1)["id"], len(payload))

    fix_dates(manifest, tmp_path)
    repaired = manifest.get_file("aaa111", 1)
    assert repaired["checksum_algo"] == "md5" and repaired["dates_fixed_at"]

    path.unlink()  # the file goes missing
    verify(manifest, tmp_path, fix=True)

    row = manifest.get_file("aaa111", 1)
    assert row["expected_size"] == len(payload)
    assert row["checksum"] == origin_etag and row["checksum_algo"] == "s3-etag"
    assert row["dates_fixed_at"] is None and row["origin_checksum"] is None


def test_the_refetched_original_is_not_judged_against_our_repaired_hash(manifest, tmp_path):
    """The consequence if it did: `verify --deep` calls the freshly downloaded
    original corrupt, deletes it, fetches it again -- forever."""
    payload = make_jpeg()
    origin_etag = _s3_etag(payload)
    item = make_item("aaa111", filename="IMG_0021.jpg", captured_at=CAPTURED_AT,
                     captured_at_timezone="Europe/Paris")
    manifest.upsert_item(item, FOLDER)
    rel = f"{FOLDER}/IMG_0021.jpg"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    manifest.upsert_file("aaa111", 1, "IMG_0021.jpg", rel, len(payload), origin_etag, "s3-etag")
    file_id = manifest.get_file("aaa111", 1)["id"]
    manifest.mark_done(file_id, len(payload))

    fix_dates(manifest, tmp_path)
    path.unlink()
    verify(manifest, tmp_path, fix=True)

    # the sync that follows writes the origin's bytes back
    path.write_bytes(payload)
    manifest.mark_done(file_id, len(payload), checksum=origin_etag, checksum_algo="s3-etag")

    report = verify(manifest, tmp_path, deep=True, fix=True)
    assert report.bad_checksum == [] and report.wrong_size == []
    assert path.exists()
