"""Token expiry mid-run must pause and resume, not lose or re-fetch work."""

import threading

import httpx
import respx
from conftest import load_fixture
from rich.console import Console

import gopro_dl.cli as cli_module
from gopro_dl.api import API_HOST
from gopro_dl.auth import AuthGate, TokenProvider, refresh_token_interactively
from gopro_dl.cli import main

GOOD = "good-token"
EXPIRED = "expired-token"
VIDEO = b"V" * 1048576
PHOTO = b"P" * 4096


class FakeConsole(Console):
    """A real rich console (the progress bar needs one) with scripted input."""

    def __init__(self, on_input):
        super().__init__(quiet=True)
        self.on_input = on_input

    def input(self, prompt="", **kwargs):
        return self.on_input()


def token_of(request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth.removeprefix("Bearer ").strip()


def test_non_interactive_refresh_picks_up_a_changed_token_file(tmp_path):
    path = tmp_path / "tok"
    path.write_text(EXPIRED)
    provider = TokenProvider(token_file=path)

    def writer():
        path.write_text(GOOD)

    threading.Timer(0.05, writer).start()
    ok = refresh_token_interactively(
        provider,
        validate=lambda t: t == GOOD,
        console=FakeConsole(lambda: ""),
        non_interactive=True,
        poll_seconds=0.05,
        timeout_seconds=5,
    )
    assert ok and provider.token == GOOD


def test_gate_trips_once_and_releases_everyone():
    gate = AuthGate()
    released = []

    def worker():
        gate.wait()
        released.append(1)

    assert gate.trip("401") is True
    assert gate.trip("401") is False  # only the first worker trips it
    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    assert released == []  # everyone is parked
    gate.resume()
    for t in threads:
        t.join(timeout=2)
    assert len(released) == 3


@respx.mock
def test_expired_token_mid_run_pauses_then_completes(tmp_path, monkeypatch):
    """The whole flow: 401 -> prompt -> new token -> resume, nothing lost."""
    token_file = tmp_path / "tok"
    token_file.write_text(EXPIRED)
    prompts = {"count": 0}

    def on_prompt():
        prompts["count"] += 1
        token_file.write_text(GOOD)  # the user pastes a fresh token
        return ""

    monkeypatch.setattr(cli_module, "console", FakeConsole(on_prompt))

    # /media/user is the validation endpoint: only the fresh token passes
    respx.get(f"{API_HOST}/media/user").mock(
        side_effect=lambda r: httpx.Response(200, json={"id": "u1"})
        if token_of(r) == GOOD
        else httpx.Response(401)
    )
    search = respx.get(f"{API_HOST}/media/search")
    search.side_effect = [
        httpx.Response(200, json=load_fixture("search_page1")),
        httpx.Response(200, json=load_fixture("search_page2")),
    ]

    def download(url, size):
        def responder(request):
            if token_of(request) != GOOD:
                return httpx.Response(401)  # the token died mid-run
            return httpx.Response(200, json={"_embedded": {"variations": [
                {"label": "source", "url": url, "file_size": size, "type": "mp4"}]}})
        return responder

    respx.get(f"{API_HOST}/media/aaa111/download").mock(side_effect=download("https://cdn.test/a", len(VIDEO)))
    respx.get(f"{API_HOST}/media/bbb222/download").mock(side_effect=download("https://cdn.test/b", len(PHOTO)))
    respx.get(f"{API_HOST}/media/ddd444/download").mock(side_effect=download("https://cdn.test/d", 2100))
    respx.get("https://cdn.test/a").mock(return_value=httpx.Response(200, content=VIDEO))
    respx.get("https://cdn.test/b").mock(return_value=httpx.Response(200, content=PHOTO))
    respx.get("https://cdn.test/d").mock(return_value=httpx.Response(200, content=b"D" * 2100))

    dest = tmp_path / "media"
    code = main([
        "sync", "--dest", str(dest), "--token-file", str(token_file),
        "--concurrency", "1", "--skip-preflight",
    ])

    assert prompts["count"] >= 1, "the run should have paused for a new token"
    assert code == 0

    # Every item still landed -- the pause cost nothing. Paths are looked up in
    # the manifest rather than hardcoded: aaa111 and ddd444 share a filename on
    # the same date, and the pause re-queued aaa111, so which of them takes the
    # plain name and which takes the id suffix depends on resolve order. Once
    # assigned, a path never moves again.
    from gopro_dl.manifest import Manifest

    with Manifest(dest / ".gopro-dl" / "manifest.db") as m:
        paths = {r["media_id"]: r["target_path"] for r in m.all_files(states=["done"])}
        assert len(paths) == 3
        assert (dest / paths["aaa111"]).read_bytes() == VIDEO
        assert (dest / paths["bbb222"]).read_bytes() == PHOTO
        assert paths["bbb222"] == "2024-01-02/GOPR0002.JPG"
        assert {paths["aaa111"], paths["ddd444"]} == {
            "2023-07-15/GX010001.MP4",
            "2023-07-15/GX010001_" + ("aaa111" if paths["aaa111"].endswith("_aaa111.MP4") else "ddd444") + ".MP4",
        }


@respx.mock
def test_cdn_403_does_not_trigger_the_token_prompt(tmp_path, monkeypatch):
    """A signed-URL expiry is routine and must not interrupt the user."""
    prompts = {"count": 0}

    def on_prompt():
        prompts["count"] += 1
        return ""

    monkeypatch.setattr(cli_module, "console", FakeConsole(on_prompt))
    respx.get(f"{API_HOST}/media/user").mock(httpx.Response(200, json={"id": "u1"}))
    search = respx.get(f"{API_HOST}/media/search")
    search.side_effect = [
        httpx.Response(200, json=load_fixture("search_page1")),
        httpx.Response(200, json=load_fixture("search_page2")),
    ]

    urls = {"aaa111": ("https://cdn.test/a", len(VIDEO)), "bbb222": ("https://cdn.test/b", len(PHOTO)),
            "ddd444": ("https://cdn.test/d", 2100)}
    for media_id, (url, size) in urls.items():
        respx.get(f"{API_HOST}/media/{media_id}/download").mock(
            httpx.Response(200, json={"_embedded": {"variations": [
                {"label": "source", "url": url, "file_size": size, "type": "mp4"}]}})
        )

    # the first hit on each CDN URL 403s (expired signature), the retry works
    state: dict[str, int] = {}

    def flaky(body):
        def responder(request):
            key = str(request.url)
            state[key] = state.get(key, 0) + 1
            if state[key] == 1:
                return httpx.Response(403)
            return httpx.Response(200, content=body)
        return responder

    respx.get("https://cdn.test/a").mock(side_effect=flaky(VIDEO))
    respx.get("https://cdn.test/b").mock(side_effect=flaky(PHOTO))
    respx.get("https://cdn.test/d").mock(side_effect=flaky(b"D" * 2100))

    dest = tmp_path / "media"
    code = main(["sync", "--dest", str(dest), "--token", GOOD, "--concurrency", "1", "--skip-preflight"])

    assert prompts["count"] == 0, "a CDN 403 must not be mistaken for token expiry"
    assert code == 0
    assert (dest / "2023-07-15" / "GX010001.MP4").read_bytes() == VIDEO
