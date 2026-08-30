"""S3 ETag verification -- the only content check GoPro makes possible."""

import hashlib

import pytest

from gopro_dl.integrity import (
    EtagVerifier,
    MultipartHasher,
    candidate_part_sizes,
    etag_for_file,
    parse_etag,
)

MIB = 1024 * 1024


def s3_etag(data: bytes, part_size: int) -> str:
    parts = [data[i : i + part_size] for i in range(0, len(data), part_size)] or [b""]
    digests = b"".join(hashlib.md5(p).digest() for p in parts)
    return f"{hashlib.md5(digests).hexdigest()}-{len(parts)}"


def test_parse_etag_handles_both_shapes():
    assert parse_etag('"abc123def4567890abc123def4567890-39"') == (
        "abc123def4567890abc123def4567890", 39,
    )
    assert parse_etag("abc123def4567890abc123def4567890") == (
        "abc123def4567890abc123def4567890", 1,
    )
    assert parse_etag("W/weak") is None
    assert parse_etag(None) is None


def test_single_part_etag_is_md5_of_the_md5_digest():
    """Verified against a real file from the live CDN: a 1-part ETag is
    md5(md5_raw(file)), NOT md5(file)."""
    data = b"gopro" * 1000
    expected = hashlib.md5(hashlib.md5(data).digest()).hexdigest() + "-1"
    hasher = MultipartHasher(len(data))
    hasher.update(data)
    assert hasher.hexdigest() == expected
    assert hashlib.md5(data).hexdigest() != expected.split("-")[0]


@pytest.mark.parametrize("part_size", [1 * MIB, 5 * MIB, 20 * MIB])
def test_multipart_hashing_matches_the_s3_formula(part_size):
    data = b"x" * int(part_size * 2.5)
    hasher = MultipartHasher(part_size)
    for i in range(0, len(data), 7777):          # arbitrary chunk boundaries
        hasher.update(data[i : i + 7777])
    assert hasher.hexdigest() == s3_etag(data, part_size)


def test_candidate_part_sizes_bracket_the_real_one():
    # the two real cases measured from the account
    assert 100 * MIB in candidate_part_sizes(4_007_119_990, 39)
    assert 20 * MIB in candidate_part_sizes(4_007_022_923, 192)
    # the window is respected: every candidate must produce that part count
    for total, parts in ((4_007_119_990, 39), (4_007_022_923, 192)):
        for size in candidate_part_sizes(total, parts):
            assert -(-total // size) == parts


def test_verifier_confirms_good_data():
    data = b"y" * (3 * MIB)
    verifier = EtagVerifier(s3_etag(data, 1 * MIB), len(data))
    verifier.update(data)
    assert verifier.result() == "ok"


def test_verifier_detects_corruption_when_the_part_size_is_unambiguous():
    data = b"y" * (2 * MIB)
    etag = s3_etag(data, 2 * MIB)
    verifier = EtagVerifier(etag, len(data))
    verifier.update(b"z" * (2 * MIB))            # same length, different bytes
    assert not verifier.ambiguous
    assert verifier.result() == "mismatch"


def test_verifier_stays_silent_rather_than_crying_corruption_when_ambiguous():
    """A non-match under several plausible part sizes may just mean an unusual
    part size, so it must not be reported as corruption."""
    total, parts = 4_007_119_990, 39
    assert len(candidate_part_sizes(total, parts)) > 1
    verifier = EtagVerifier(f"{'0'*32}-{parts}", total)
    verifier.update(b"nowhere near the real bytes")
    assert verifier.ambiguous
    assert verifier.result() is None


def test_etag_for_file_round_trip(tmp_path):
    data = b"z" * (5 * MIB + 12345)
    path = tmp_path / "clip.mp4"
    path.write_bytes(data)
    assert etag_for_file(path, s3_etag(data, 2 * MIB)) == "ok"
    path.write_bytes(b"w" * len(data))
    assert etag_for_file(path, s3_etag(data, len(data))) == "mismatch"
