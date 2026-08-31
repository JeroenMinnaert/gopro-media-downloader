"""What eight workers do to one manifest.

The design stakes "two workers never race on the same file" on SQLite's own
locking (`claim_item`/`claim_file`), and the rest of the suite runs at a
concurrency of one or two against responses that return instantly -- so the
contention those claims exist for never actually happens. These tests make it
happen.
"""

import threading
import time

import httpx
import respx
from conftest import make_item

from gopro_dl.api import API_HOST
from gopro_dl.cli import main
from gopro_dl.manifest import Manifest

ITEMS = 24
BODY = b"C" * 4096


def test_only_one_of_eight_threads_can_claim_a_file(manifest):
    """The claim is the lock. If two threads could hold one row, two workers
    would write the same file at the same time."""
    manifest.upsert_item(make_item("aaa111"), "2023-07-15")
    file_id = manifest.upsert_file("aaa111", 1, "a.MP4", "2023-07-15/a.MP4", 1000)

    start = threading.Barrier(8)
    won: list[bool] = []
    lock = threading.Lock()

    def contend():
        start.wait()  # everyone reaches for the same row at once
        got = manifest.claim_file(file_id)
        with lock:
            won.append(got)

    threads = [threading.Thread(target=contend) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert won.count(True) == 1, "exactly one worker may hold a file"
    assert manifest.get_file("aaa111", 1)["attempts"] == 1, "only the winner is charged"


def test_only_one_of_eight_threads_can_claim_an_item(manifest):
    manifest.upsert_item(make_item("aaa111"), "2023-07-15")

    start = threading.Barrier(8)
    won: list[bool] = []
    lock = threading.Lock()

    def contend():
        start.wait()
        got = manifest.claim_item("aaa111")
        with lock:
            won.append(got)

    threads = [threading.Thread(target=contend) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert won.count(True) == 1


def _library(count: int) -> dict:
    return {
        "_pages": {"current_page": 1, "per_page": count, "total_items": count,
                   "total_pages": 1},
        "_embedded": {"media": [
            {"id": f"item{n:03d}", "type": "Photo", "filename": f"GOPR{n:04d}.JPG",
             "captured_at": "2024-01-02T09:15:00Z", "captured_at_timezone": "+01:00",
             "file_size": len(BODY), "item_count": 1}
            for n in range(count)
        ]},
    }


@respx.mock
def test_a_full_pool_downloads_every_file_exactly_once(tmp_path):
    """Eight workers, twenty-four items, and responses slow enough that the
    pool is genuinely busy at once rather than serialised by luck."""
    respx.get(f"{API_HOST}/media/user").mock(httpx.Response(200, json={"id": "u1"}))
    respx.get(f"{API_HOST}/media/search").mock(
        httpx.Response(200, json=_library(ITEMS))
    )

    inflight = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def slow_cdn(request):
        with lock:
            inflight["now"] += 1
            inflight["peak"] = max(inflight["peak"], inflight["now"])
        time.sleep(0.05)  # long enough that the workers overlap
        with lock:
            inflight["now"] -= 1
        return httpx.Response(200, content=BODY)

    for n in range(ITEMS):
        respx.get(f"{API_HOST}/media/item{n:03d}/download").mock(
            httpx.Response(200, json={"_embedded": {"variations": [
                {"label": "source", "url": f"https://cdn.gopro.test/{n}",
                 "file_size": len(BODY), "type": "jpg"}]}})
        )
        respx.get(f"https://cdn.gopro.test/{n}").mock(side_effect=slow_cdn)

    dest = tmp_path / "media"
    assert main(["sync", "--dest", str(dest), "--token", "test-token",
                 "--concurrency", "8"]) == 0

    assert inflight["peak"] > 1, "the pool never actually ran in parallel"

    cdn = [c for c in respx.calls if c.request.url.host == "cdn.gopro.test"]
    assert len(cdn) == ITEMS, "a file was fetched twice"
    assert len({str(c.request.url) for c in cdn}) == ITEMS

    files = sorted(p for p in dest.rglob("*.JPG"))
    assert len(files) == ITEMS
    assert all(p.read_bytes() == BODY for p in files)
    assert not list(dest.rglob("*.part"))

    with Manifest(dest / ".gopro-dl" / "manifest.db") as m:
        rows = m.all_files()
        assert len(rows) == ITEMS
        assert all(r["state"] == "done" for r in rows)
        assert all(r["attempts"] == 1 for r in rows), "a row was claimed more than once"


@respx.mock
def test_a_requeued_item_is_not_taken_twice_while_a_worker_holds_it(manifest):
    """An auth pause puts an item back on the queue while its worker is still
    unwinding, so the same id can be queued twice. The claim is what stops a
    second worker picking it up."""
    manifest.upsert_item(make_item("aaa111"), "2023-07-15")
    assert manifest.claim_item("aaa111") is True     # the first worker holds it
    assert manifest.claim_item("aaa111") is False    # the duplicate is refused
    manifest.release_item("aaa111", charge_attempt=False)
    assert manifest.claim_item("aaa111") is True     # ...and free again after
