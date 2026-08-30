"""Full `gopro-dl sync` against a mocked GoPro: enumerate -> download -> resume."""

import httpx
import respx
from conftest import load_fixture

from gopro_dl.api import API_HOST
from gopro_dl.cli import main
from gopro_dl.manifest import Manifest

VIDEO = b"V" * 1048576   # matches file_size in search_page1 for aaa111
PHOTO = b"P" * 4096      # matches bbb222
CHAPTERS = {1: b"1" * 800, 2: b"2" * 700, 3: b"3" * 600}


def mock_gopro(chaptered=True):
    """Wire up the whole API surface the tool touches."""
    respx.get(f"{API_HOST}/media/user").mock(httpx.Response(200, json={"id": "u1", "email": "j@example.com"}))

    search = respx.get(f"{API_HOST}/media/search")
    search.side_effect = [
        httpx.Response(200, json=load_fixture("search_page1")),
        httpx.Response(200, json=load_fixture("search_page2")),
    ]

    def variation(url, size):
        return {"_embedded": {"variations": [
            {"label": "mp4_low", "url": "https://cdn.gopro.test/low", "file_size": 10},
            {"label": "source", "url": url, "file_size": size, "type": "mp4"},
        ]}}

    respx.get(f"{API_HOST}/media/aaa111/download").mock(
        httpx.Response(200, json=variation("https://cdn.gopro.test/aaa111", len(VIDEO)))
    )
    respx.get(f"{API_HOST}/media/bbb222/download").mock(
        httpx.Response(200, json=variation("https://cdn.gopro.test/bbb222", len(PHOTO)))
    )
    # A chaptered recording: several variations all labelled "source", told
    # apart only by the trailing number in the URL -- and an _embedded.files
    # entry pointing at the proxy, which must be ignored.
    respx.get(f"{API_HOST}/media/ddd444/download").mock(
        httpx.Response(200, json={"_embedded": {
            "files": [{"item_number": 1, "url": "https://cdn.gopro.test/ddd444-proxy"}],
            "variations": [
                {"label": "source", "url": f"https://cdn.gopro.test/ddd444/source/default/{n}.mp4",
                 "type": "mp4"}
                for n in CHAPTERS
            ] + [{"label": "high_res_proxy_mp4", "url": "https://cdn.gopro.test/ddd444-proxy"}],
        }})
    )

    def ranged(body):
        def responder(request):
            rng = request.headers.get("Range")
            if not rng:
                return httpx.Response(200, content=body)
            start = int(rng.split("=")[1].split("-")[0])
            return httpx.Response(206, content=body[start:])
        return responder

    respx.get("https://cdn.gopro.test/aaa111").mock(side_effect=ranged(VIDEO))
    respx.get("https://cdn.gopro.test/bbb222").mock(side_effect=ranged(PHOTO))
    for n, body in CHAPTERS.items():
        respx.get(f"https://cdn.gopro.test/ddd444/source/default/{n}.mp4").mock(side_effect=ranged(body))
    # if this is ever requested the proxy bug is back
    respx.get("https://cdn.gopro.test/ddd444-proxy").mock(
        side_effect=AssertionError("downloaded the proxy instead of the source")
    )


def run(dest, *extra):
    return main(["sync", "--dest", str(dest), "--token", "test-token",
                 "--concurrency", "2", *extra])


@respx.mock
def test_full_sync_lays_out_flat_date_folders(tmp_path):
    mock_gopro()
    dest = tmp_path / "media"
    assert run(dest) == 0

    # aaa111 was captured 22:30 UTC in Brussels -> next local day
    assert (dest / "2023-07-15" / "GX010001.MP4").read_bytes() == VIDEO
    assert (dest / "2024-01-02" / "GOPR0002.JPG").read_bytes() == PHOTO
    # ddd444 shares a filename with aaa111 on the same date -> id-suffixed
    assert (dest / "2023-07-15" / "GX010001_ddd444.MP4").read_bytes() == CHAPTERS[1]
    assert (dest / "2023-07-15" / "GX020001.MP4").read_bytes() == CHAPTERS[2]
    assert (dest / "2023-07-15" / "GX030001.MP4").read_bytes() == CHAPTERS[3]
    # the chapters sum to the size the listing advertised
    assert sum(len(b) for b in CHAPTERS.values()) == 2100

    # the GoPro-generated edit was never fetched
    assert not list(dest.glob("**/MyEdit.MP4"))
    assert not list(dest.glob("**/*.part"))


@respx.mock
def test_second_run_is_a_no_op(tmp_path):
    mock_gopro()
    dest = tmp_path / "media"
    assert run(dest) == 0
    def media():
        return {
            q: q.stat().st_mtime_ns
            for q in dest.rglob("*")
            if q.is_file() and ".gopro-dl" not in q.parts
        }

    before = media()

    mock_gopro()  # fresh pagination side effects for the second run
    cdn_calls_before = sum(
        c.request.url.host == "cdn.gopro.test" for c in respx.calls
    )
    assert run(dest) == 0
    cdn_calls_after = sum(c.request.url.host == "cdn.gopro.test" for c in respx.calls)

    after = media()
    assert before == after, "a re-run must not rewrite files"
    assert cdn_calls_after == cdn_calls_before, "a re-run must not re-download"


@respx.mock
def test_dry_run_downloads_nothing(tmp_path):
    mock_gopro()
    dest = tmp_path / "media"
    assert run(dest, "--dry-run") == 0
    assert not list(dest.glob("**/*.MP4"))
    # but the manifest is fully built, ready for the real run
    with Manifest(dest / ".gopro-dl" / "manifest.db") as m:
        assert len(m.pending_items()) == 3          # 4 items, 1 is an edit
        assert m.remaining_bytes() > 0


@respx.mock
def test_limit_caps_the_run(tmp_path):
    mock_gopro()
    dest = tmp_path / "media"
    assert run(dest, "--limit", "1") == 0
    assert len(list(dest.rglob("*.MP4")) + list(dest.rglob("*.JPG"))) == 1


@respx.mock
def test_resume_after_an_interrupted_file(tmp_path):
    mock_gopro()
    dest = tmp_path / "media"
    # a half-written file from a previous run
    part = dest / "2023-07-15" / "GX010001.MP4.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(VIDEO[:500000])

    assert run(dest) == 0
    assert (dest / "2023-07-15" / "GX010001.MP4").read_bytes() == VIDEO
    ranges = [c.request.headers.get("Range") for c in respx.calls
              if c.request.url.host == "cdn.gopro.test"]
    assert "bytes=500000-" in ranges


@respx.mock
def test_small_disk_does_not_block_dry_run_or_a_limited_run(tmp_path, monkeypatch):
    """A local disk smaller than the library must not break the smoke-test flow.

    The user's workflow is download-locally-then-rsync, so their scratch disk is
    plausibly smaller than the whole ~1 TB library. Planning and a --limit run
    are sized against what they will actually fetch.
    """
    import shutil
    import subprocess

    mock_gopro()
    dest = tmp_path / "media"
    real_usage = shutil.disk_usage

    def tiny(path):
        usage = real_usage(path)
        return type(usage)(usage.total, usage.used, 8 * 1024**3)  # 8 GiB free

    class TinyDf:
        stdout = (
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            f"/dev/disk1 20000000 11611392 {8 * 1024**2} 60% /\n"
        )

    monkeypatch.setattr(shutil, "disk_usage", tiny)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: TinyDf())

    assert run(dest, "--dry-run") == 0
    mock_gopro()  # re-arm the paginated search for a second run
    assert run(dest, "--limit", "1") == 0
    assert len(list(dest.rglob("*.MP4")) + list(dest.rglob("*.JPG"))) == 1


@respx.mock
def test_genuinely_full_disk_is_still_refused(tmp_path, monkeypatch):
    """Both space sources must report full: free_space() takes the larger of
    df and statvfs, so stubbing only one would leave the real disk visible."""
    import shutil
    import subprocess

    mock_gopro()
    dest = tmp_path / "media"
    real_usage = shutil.disk_usage

    def full(path):
        usage = real_usage(path)
        return type(usage)(usage.total, usage.used, 1024)  # 1 KiB free

    class FullDf:
        stdout = (
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "/dev/disk1 1000000 999999 1 100% /\n"
        )

    monkeypatch.setattr(shutil, "disk_usage", full)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FullDf())

    assert run(dest) == 1
    assert not list(dest.rglob("*.MP4"))
