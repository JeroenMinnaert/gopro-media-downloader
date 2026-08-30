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
