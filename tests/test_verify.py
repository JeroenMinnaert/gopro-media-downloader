"""The verify pass: catches truncation, deletion and bad hashes after the fact."""

import hashlib

from conftest import make_item

from gopro_dl.verify import verify

CONTENT = b"x" * 500


def seed_done(manifest, dest, checksum=None, algo=None):
    manifest.upsert_item(make_item("aaa111"), "2023-07-15")
    manifest.upsert_file(
        "aaa111", 1, "GX010001.MP4", "2023-07-15/GX010001.MP4", len(CONTENT), checksum, algo
    )
    row = manifest.get_file("aaa111", 1)
    manifest.mark_done(row["id"], len(CONTENT))
    path = dest / "2023-07-15" / "GX010001.MP4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(CONTENT)
    return path


def test_healthy_file_passes(manifest, tmp_path):
    seed_done(manifest, tmp_path)
    report = verify(manifest, tmp_path)
    assert report.checked == 1 and report.ok == 1 and report.problems == 0


def test_truncated_file_is_caught_and_requeued_with_fix(manifest, tmp_path):
    path = seed_done(manifest, tmp_path)
    path.write_bytes(CONTENT[:100])

    report = verify(manifest, tmp_path, fix=True)
    assert report.wrong_size and report.problems == 1
    assert manifest.get_file("aaa111", 1)["state"] == "pending"
    assert not path.exists()  # removed so the re-download starts clean


def test_deleted_file_is_caught(manifest, tmp_path):
    path = seed_done(manifest, tmp_path)
    path.unlink()

    report = verify(manifest, tmp_path, fix=True)
    assert report.missing == ["2023-07-15/GX010001.MP4"]
    assert manifest.get_file("aaa111", 1)["state"] == "pending"


def test_deep_verify_checks_the_hash_when_one_is_known(manifest, tmp_path):
    digest = hashlib.md5(CONTENT).hexdigest()
    path = seed_done(manifest, tmp_path, checksum=digest, algo="md5")
    assert verify(manifest, tmp_path, deep=True).ok == 1

    # same length, different bytes: only a hash can catch this
    path.write_bytes(b"y" * 500)
    report = verify(manifest, tmp_path, deep=True)
    assert report.bad_checksum == ["2023-07-15/GX010001.MP4"]


def s3_etag(body: bytes, part_size: int | None = None) -> str:
    size = part_size or max(len(body), 1)
    digests = [hashlib.md5(body[i:i + size]).digest() for i in range(0, len(body), size)]
    return f"{hashlib.md5(b''.join(digests)).hexdigest()}-{len(digests)}"


def test_deep_verify_rehashes_against_the_origin_etag(manifest, tmp_path):
    """The primary integrity path: what `backfill-etags` fills in is what
    `verify --deep` checks."""
    path = seed_done(manifest, tmp_path, checksum=s3_etag(CONTENT), algo="s3-etag")
    assert verify(manifest, tmp_path, deep=True).ok == 1

    path.write_bytes(b"y" * len(CONTENT))  # same length, different bytes
    report = verify(manifest, tmp_path, deep=True, fix=True)
    assert report.bad_checksum == ["2023-07-15/GX010001.MP4"]
    assert manifest.get_file("aaa111", 1)["state"] == "pending"
    assert not path.exists()


def test_an_etag_with_no_pinnable_part_size_is_unverifiable_not_corrupt(manifest, tmp_path):
    """Several part sizes fit a 2-part 5 MiB object, so a non-match proves
    nothing. Calling that corruption would delete a perfectly good file."""
    big = b"B" * (5 * 1024 * 1024)
    # A 2-part ETag whose hash matches no candidate size: 3, 4 and 5 MiB all
    # fit the part count, so a non-match may just be an unusual part size.
    manifest.upsert_item(make_item("bbb222"), "2023-07-15")
    manifest.upsert_file(
        "bbb222", 1, "GX010002.MP4", "2023-07-15/GX010002.MP4", len(big),
        "0" * 32 + "-2", "s3-etag",
    )
    row = manifest.get_file("bbb222", 1)
    manifest.mark_done(row["id"], len(big))
    path = tmp_path / "2023-07-15" / "GX010002.MP4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(big)

    report = verify(manifest, tmp_path, deep=True, fix=True)
    assert report.bad_checksum == [] and report.problems == 0
    assert "2023-07-15/GX010002.MP4" in report.unverifiable
    assert path.exists()


def test_a_file_with_nothing_to_check_against_is_not_counted_as_passing(manifest, tmp_path):
    seed_done(manifest, tmp_path)
    manifest.conn.execute("UPDATE media_files SET expected_size=NULL")
    manifest.conn.commit()

    report = verify(manifest, tmp_path)
    assert report.unverifiable == ["2023-07-15/GX010001.MP4"]
    assert report.ok == 0, "no size, no checksum: there is no verdict to report"


def test_an_api_supplied_etag_is_honoured_like_an_s3_one(manifest, tmp_path):
    """`models._checksum_of` stores an API `etag` field under the algo name
    "etag"; skipping it would leave those files silently unchecked."""
    path = seed_done(manifest, tmp_path, checksum=s3_etag(CONTENT), algo="etag")
    assert verify(manifest, tmp_path, deep=True).ok == 1

    path.write_bytes(b"z" * len(CONTENT))
    assert verify(manifest, tmp_path, deep=True).bad_checksum == ["2023-07-15/GX010001.MP4"]


def test_a_requeued_file_gets_its_attempts_back(manifest, tmp_path):
    """`verify --fix` says "run sync"; sync skips files that used up their
    attempt budget, so the requeue has to clear it or nothing happens."""
    path = seed_done(manifest, tmp_path)
    row = manifest.get_file("aaa111", 1)
    manifest.conn.execute("UPDATE media_files SET attempts=9 WHERE id=?", (row["id"],))
    manifest.conn.commit()
    path.unlink()

    verify(manifest, tmp_path, fix=True)
    after = manifest.get_file("aaa111", 1)
    assert after["state"] == "pending" and after["attempts"] == 0


def test_a_deep_verify_records_the_proof_it_computed(manifest, tmp_path):
    """Otherwise a resumed file -- which cannot be hashed while streaming --
    stays "size-only" in `status` no matter how often it is re-hashed."""
    seed_done(manifest, tmp_path, checksum=s3_etag(CONTENT), algo="s3-etag")
    file_id = manifest.get_file("aaa111", 1)["id"]
    manifest.set_checksum(file_id, s3_etag(CONTENT), "s3-etag", state="unverified")

    assert verify(manifest, tmp_path, deep=True).ok == 1
    assert manifest.get_file("aaa111", 1)["checksum_state"] == "ok"


def test_a_deep_verify_does_not_overwrite_the_date_repair_verdict(manifest, tmp_path):
    """`local_after_date_fix` says something different and still true."""
    digest = hashlib.md5(CONTENT).hexdigest()
    seed_done(manifest, tmp_path, checksum=digest, algo="md5")
    file_id = manifest.get_file("aaa111", 1)["id"]
    manifest.set_checksum(file_id, digest, "md5", state="local_after_date_fix")

    verify(manifest, tmp_path, deep=True)
    assert manifest.get_file("aaa111", 1)["checksum_state"] == "local_after_date_fix"
