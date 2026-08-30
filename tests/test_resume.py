"""The download state machine: Range resume, ignored Range, expired URLs, verify."""

import threading

import httpx
import pytest
import respx
from conftest import load_fixture, make_item

from gopro_dl.api import API_HOST
from gopro_dl.downloader import Downloader, ShuttingDown

CONTENT = bytes(range(256)) * 8  # 2048 deterministic bytes
CDN = "https://cdn.gopro.test/source.mp4"
FIXTURE_SOURCE_URL = (
    "https://media-cdn-vod.gopro.test/user/1234567890123456789/source/default/1.mp4"
)


def build(manifest, tmp_path, client, shutdown=None):
    return Downloader(
        client=client,
        manifest=manifest,
        dest=tmp_path / "media",
        shutdown=shutdown or threading.Event(),
    )


def seed(manifest, tmp_path, size=len(CONTENT), media_id="aaa111"):
    """One item with one file row, ready to download."""
    item = make_item(media_id)
    manifest.upsert_item(item, "2023-07-15")
    manifest.upsert_file(media_id, 1, "GX010001.MP4", "2023-07-15/GX010001.MP4", size)
    return item, manifest.get_file(media_id, 1)


def source_of(item, size=len(CONTENT), url=CDN):
    from gopro_dl.models import SourceFile

    return SourceFile(item_number=1, filename="GX010001.MP4", url=url, size=size)


def ranged(request):
    """A well-behaved CDN that honours Range."""
    rng = request.headers.get("Range")
    if not rng:
        return httpx.Response(200, content=CONTENT)
    start = int(rng.split("=")[1].split("-")[0])
    body = CONTENT[start:]
    return httpx.Response(
        206,
        content=body,
        headers={"Content-Range": f"bytes {start}-{len(CONTENT)-1}/{len(CONTENT)}"},
    )


@respx.mock
def test_fresh_download_verifies_and_finalises(manifest, tmp_path, client):
    respx.get(CDN).mock(side_effect=ranged)
    item, row = seed(manifest, tmp_path)
    outcome = build(manifest, tmp_path, client).fetch_file(item, source_of(item), row, "2023-07-15")

    final = tmp_path / "media" / "2023-07-15" / "GX010001.MP4"
    assert outcome.state == "done"
    assert final.read_bytes() == CONTENT
    assert not final.with_suffix(".MP4.part").exists()  # .part cleaned up by the rename


@respx.mock
def test_resume_continues_from_the_part_offset(manifest, tmp_path, client):
    route = respx.get(CDN).mock(side_effect=ranged)
    item, row = seed(manifest, tmp_path)

    # simulate an interrupted run: 800 of 2048 bytes already on disk
    part = tmp_path / "media" / "2023-07-15" / "GX010001.MP4.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(CONTENT[:800])

    outcome = build(manifest, tmp_path, client).fetch_file(item, source_of(item), row, "2023-07-15")

    assert outcome.state == "done"
    assert route.calls[0].request.headers["Range"] == "bytes=800-"
    assert (tmp_path / "media" / "2023-07-15" / "GX010001.MP4").read_bytes() == CONTENT
    # only the missing tail was transferred
    assert outcome.bytes_written == len(CONTENT) - 800


@respx.mock
def test_server_ignoring_range_restarts_instead_of_corrupting(manifest, tmp_path, client):
    # Returns the whole body with 200 even though we asked for a range. Naively
    # appending would produce a file that is too long and silently corrupt.
    respx.get(CDN).mock(return_value=httpx.Response(200, content=CONTENT))
    item, row = seed(manifest, tmp_path)
    part = tmp_path / "media" / "2023-07-15" / "GX010001.MP4.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(CONTENT[:800])

    outcome = build(manifest, tmp_path, client).fetch_file(item, source_of(item), row, "2023-07-15")

    assert outcome.state == "done"
    assert (tmp_path / "media" / "2023-07-15" / "GX010001.MP4").read_bytes() == CONTENT


@respx.mock
def test_416_on_a_complete_part_finalises_it(manifest, tmp_path, client):
    # No size is known, so the run cannot short-circuit -- it asks for the tail
    # of a .part that is in fact already complete and gets 416 back.
    route = respx.get(CDN).mock(return_value=httpx.Response(416))
    item, row = seed(manifest, tmp_path, size=None)
    part = tmp_path / "media" / "2023-07-15" / "GX010001.MP4.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(CONTENT)

    outcome = build(manifest, tmp_path, client).fetch_file(
        item, source_of(item, size=None), row, "2023-07-15"
    )
    assert route.called and route.calls[0].request.headers["Range"] == f"bytes={len(CONTENT)}-"
    assert outcome.state == "done"
    assert outcome.reason == "size_unverified"
    assert (tmp_path / "media" / "2023-07-15" / "GX010001.MP4").read_bytes() == CONTENT


@respx.mock
def test_expired_signed_url_is_refreshed_not_failed(manifest, tmp_path, client):
    # First attempt: the signature has expired. The fix is a fresh URL from the
    # API, not marking the file failed.
    fresh_url = "https://cdn.gopro.test/fresh.mp4"
    respx.get(CDN).mock(return_value=httpx.Response(403))
    respx.get(fresh_url).mock(side_effect=ranged)
    download = load_fixture("download_single")
    source = next(v for v in download["_embedded"]["variations"] if v["label"] == "source")
    source["url"] = fresh_url
    api = respx.get(f"{API_HOST}/media/aaa111/download").mock(httpx.Response(200, json=download))

    item, row = seed(manifest, tmp_path)
    outcome = build(manifest, tmp_path, client).fetch_file(item, source_of(item), row, "2023-07-15")

    assert outcome.state == "done"
    assert api.called
    assert (tmp_path / "media" / "2023-07-15" / "GX010001.MP4").read_bytes() == CONTENT


@respx.mock
def test_short_body_fails_verification_and_leaves_no_final_file(manifest, tmp_path, client):
    respx.get(CDN).mock(return_value=httpx.Response(200, content=CONTENT[:100]))
    item, row = seed(manifest, tmp_path)
    outcome = build(manifest, tmp_path, client).fetch_file(item, source_of(item), row, "2023-07-15")

    assert outcome.state == "failed" and "size mismatch" in outcome.reason
    assert not (tmp_path / "media" / "2023-07-15" / "GX010001.MP4").exists()
    # the partial data is kept so the next run can resume rather than restart
    assert (tmp_path / "media" / "2023-07-15" / "GX010001.MP4.part").exists()


@respx.mock
def test_correctly_sized_existing_file_is_not_redownloaded(manifest, tmp_path, client):
    route = respx.get(CDN).mock(side_effect=ranged)
    item, row = seed(manifest, tmp_path)
    final = tmp_path / "media" / "2023-07-15" / "GX010001.MP4"
    final.parent.mkdir(parents=True)
    final.write_bytes(CONTENT)

    outcome = build(manifest, tmp_path, client).fetch_file(item, source_of(item), row, "2023-07-15")

    assert outcome.state == "done" and outcome.reason == "already_on_disk"
    assert not route.called  # no bytes over the wire


@respx.mock
def test_wrong_sized_existing_file_is_replaced(manifest, tmp_path, client):
    respx.get(CDN).mock(side_effect=ranged)
    item, row = seed(manifest, tmp_path)
    final = tmp_path / "media" / "2023-07-15" / "GX010001.MP4"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"truncated")

    outcome = build(manifest, tmp_path, client).fetch_file(item, source_of(item), row, "2023-07-15")
    assert outcome.state == "done"
    assert final.read_bytes() == CONTENT


@respx.mock
def test_shutdown_midstream_keeps_the_partial_file(manifest, tmp_path, client):
    """Ctrl-C during a large file must keep the bytes already fetched."""
    from gopro_dl.downloader import CHUNK_SIZE

    shutdown = threading.Event()
    big = b"A" * (CHUNK_SIZE * 3)

    def body():
        yield big[:CHUNK_SIZE]
        shutdown.set()          # the user presses Ctrl-C mid-transfer
        yield big[CHUNK_SIZE:]

    respx.get(CDN).mock(return_value=httpx.Response(200, content=body()))
    item, row = seed(manifest, tmp_path, size=len(big))

    with pytest.raises(ShuttingDown):
        build(manifest, tmp_path, client, shutdown).fetch_file(
            item, source_of(item, size=len(big)), row, "2023-07-15"
        )

    part = tmp_path / "media" / "2023-07-15" / "GX010001.MP4.part"
    assert part.exists() and 0 < part.stat().st_size < len(big)
    # nothing half-written was ever presented under the real name
    assert not (tmp_path / "media" / "2023-07-15" / "GX010001.MP4").exists()


@respx.mock
def test_chapters_each_get_their_own_row_and_path(manifest, tmp_path, client):
    respx.get(f"{API_HOST}/media/ddd444/download").mock(
        httpx.Response(200, json=load_fixture("download_chaptered"))
    )
    item = make_item("ddd444", item_count=3)
    manifest.upsert_item(item, "2023-07-15")
    files, skip = build(manifest, tmp_path, client).resolve(item, "2023-07-15")

    assert skip is None and len(files) == 3
    rows = manifest.files_for("ddd444")
    assert [r["target_path"] for r in rows] == [
        "2023-07-15/GX010001.MP4",
        "2023-07-15/GX020001.MP4",
        "2023-07-15/GX030001.MP4",
    ]


@respx.mock
def test_stale_listing_size_yields_to_the_origin(manifest, tmp_path, client):
    """GoPro's listing file_size is sometimes a few KB larger than the object S3
    actually holds. The origin wins, otherwise the file fails forever.

    Sized like the real cases: a 3280-byte overstatement on a 5 MiB file.
    """
    big = bytes(range(256)) * 20480  # 5 MiB
    respx.get(CDN).mock(return_value=httpx.Response(200, content=big))
    listed = len(big) + 3280
    item, row = seed(manifest, tmp_path, size=listed)

    outcome = build(manifest, tmp_path, client).fetch_file(
        item, source_of(item, size=listed), row, "2023-07-15"
    )

    final = tmp_path / "media" / "2023-07-15" / "GX010001.MP4"
    assert outcome.state == "done"
    assert outcome.size == len(big)
    assert final.read_bytes() == big


@respx.mock
def test_short_body_against_the_origin_size_still_fails(manifest, tmp_path, client):
    """Yielding to the origin must not swallow a genuinely truncated transfer:
    the server advertises the full length and then delivers less."""
    respx.get(CDN).mock(
        return_value=httpx.Response(
            200,
            content=CONTENT[:1000],
            headers={"Content-Length": str(len(CONTENT))},
        )
    )
    item, row = seed(manifest, tmp_path)

    outcome = build(manifest, tmp_path, client).fetch_file(
        item, source_of(item), row, "2023-07-15"
    )

    assert outcome.state == "failed"
    assert "size mismatch" in outcome.reason


@respx.mock
def test_connection_drop_midstream_resumes_instead_of_crashing(manifest, tmp_path, client):
    """A dropped connection returns a StreamResult, not a tuple -- the caller
    reads .written off it, so the wrong shape crashed the whole resume path."""
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadError("connection reset")
        return ranged(request)

    respx.get(CDN).mock(side_effect=flaky)
    respx.get(f"{API_HOST}/media/aaa111/download").mock(
        return_value=httpx.Response(200, json=load_fixture("download_single"))
    )
    # the refreshed signed URL the download endpoint hands back
    respx.get(FIXTURE_SOURCE_URL).mock(side_effect=ranged)
    item, row = seed(manifest, tmp_path)

    outcome = build(manifest, tmp_path, client).fetch_file(
        item, source_of(item), row, "2023-07-15"
    )

    assert outcome.state == "done"
    assert (tmp_path / "media" / "2023-07-15" / "GX010001.MP4").read_bytes() == CONTENT


@respx.mock
def test_origin_size_far_below_the_listing_is_not_tolerated(manifest, tmp_path, client):
    """Only metadata-scale drift yields to the origin. A server advertising a
    fraction of the expected size is data loss and must still fail."""
    respx.get(CDN).mock(return_value=httpx.Response(200, content=CONTENT[:100]))
    item, row = seed(manifest, tmp_path, size=10 * 1024 * 1024)

    outcome = build(manifest, tmp_path, client).fetch_file(
        item, source_of(item, size=10 * 1024 * 1024), row, "2023-07-15"
    )

    assert outcome.state == "failed" and "size mismatch" in outcome.reason


@respx.mock
def test_corrected_size_is_persisted_so_verify_stays_clean(manifest, tmp_path, client):
    """The stale expectation must not survive in the manifest: `verify` compares
    on-disk bytes against expected_size, and `verify --fix` would delete the good
    file and fetch it again on every run."""
    from gopro_dl.verify import verify as run_verify

    big = bytes(range(256)) * 20480  # 5 MiB
    respx.get(CDN).mock(return_value=httpx.Response(200, content=big))
    listed = len(big) + 3280
    item, row = seed(manifest, tmp_path, size=listed)

    runner_row = manifest.get_file(item.id, 1)
    outcome = build(manifest, tmp_path, client).fetch_file(
        item, source_of(item, size=listed), row, "2023-07-15"
    )
    assert outcome.size_corrected
    manifest.mark_done(runner_row["id"], outcome.size, size_corrected=outcome.size_corrected)

    stored = manifest.get_file(item.id, 1)
    assert stored["expected_size"] == len(big)

    report = run_verify(manifest, tmp_path / "media")
    assert report.wrong_size == []


@respx.mock
def test_good_file_on_disk_survives_a_stale_rebuilt_manifest(manifest, tmp_path, client):
    """A rebuilt manifest re-reads file_size from the API. The file already on
    disk is correct, so it must be kept rather than deleted and re-downloaded."""
    big = bytes(range(256)) * 20480  # 5 MiB
    route = respx.get(CDN).mock(return_value=httpx.Response(200, content=big))
    listed = len(big) + 3280
    item, row = seed(manifest, tmp_path, size=listed)

    final = tmp_path / "media" / "2023-07-15" / "GX010001.MP4"
    final.parent.mkdir(parents=True)
    final.write_bytes(big)

    outcome = build(manifest, tmp_path, client).fetch_file(
        item, source_of(item, size=listed), row, "2023-07-15"
    )

    assert outcome.state == "done" and outcome.reason == "already_on_disk"
    assert outcome.size_corrected
    assert not route.called  # nothing re-downloaded
    assert final.read_bytes() == big
