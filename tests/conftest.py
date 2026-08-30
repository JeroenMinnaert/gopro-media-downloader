import json
import os
import threading
from pathlib import Path

import pytest

from gopro_dl.api import GoProClient
from gopro_dl.auth import AuthGate, TokenProvider
from gopro_dl.circuit import CircuitBreaker
from gopro_dl.locations import AppDirs
from gopro_dl.manifest import Manifest
from gopro_dl.models import MediaItem

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch, tmp_path):
    """Never let a real configuration -- or the real machine's app-dir state
    -- steer the tests.

    A developer's exported GOPRO_* variables would otherwise redirect the
    destination and -- far worse -- the manifest, so tests would write their
    fixtures into a real library's database. Likewise, gopro-dl's own app
    directory (token, config file, browser profile, NAS-redirect manifests --
    everything `AppDirs` resolves) and default destination (~/Downloads/GoPro)
    must never resolve to something real on the machine running the tests --
    a test that forgets to override them should get an isolated fake, not
    silently touch a real file. One patch point for the whole app directory
    (rather than one per file it contains) because `AppDirs` is resolved
    explicitly and passed down, not read from scattered module constants.
    """
    for key in list(os.environ):
        if key.startswith("GOPRO_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("gopro_dl.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr("gopro_dl.config.default_dest", lambda: tmp_path / "unset-default-dest")
    monkeypatch.setattr(AppDirs, "resolve", lambda: AppDirs(root=tmp_path / "unset-app-dir"))


@pytest.fixture(autouse=True)
def no_real_browser_automation(monkeypatch):
    """Never let a test launch a real Playwright browser.

    auth.py and cli.py each bind their own `fetch_cached_browser_token` /
    `login_via_browser` names at import time, so all four call sites need
    patching -- patching browser_login.fetch_cached/login itself would not
    reach them. Defaults to "nothing cached, login cancelled"; tests that
    exercise the browser-login path override these explicitly.
    """
    def no_cached(*a, **k):
        return None

    def no_login(*a, **k):
        return None

    monkeypatch.setattr("gopro_dl.auth.fetch_cached_browser_token", no_cached)
    monkeypatch.setattr("gopro_dl.auth.login_via_browser", no_login)
    monkeypatch.setattr("gopro_dl.cli.fetch_cached_browser_token", no_cached)
    monkeypatch.setattr("gopro_dl.cli.login_via_browser", no_login)


@pytest.fixture
def manifest(tmp_path) -> Manifest:
    m = Manifest(tmp_path / "manifest.db")
    yield m
    m.close()


@pytest.fixture
def client(tmp_path):
    tokens = TokenProvider(token="test-token")
    c = GoProClient(
        tokens=tokens,
        gate=AuthGate(),
        breaker=CircuitBreaker(),
        shutdown=threading.Event(),
        max_attempts=3,
        sleep=lambda _: None,  # no real backoff waits in tests
    )
    yield c
    c.close()


def make_item(media_id="aaa111", **overrides) -> MediaItem:
    data = {
        "id": media_id,
        "type": "Video",
        "filename": "GX010001.MP4",
        "captured_at": "2023-07-14T22:30:00Z",
        "captured_at_timezone": "Europe/Brussels",
        "file_size": 1000,
        "item_count": 1,
    }
    data.update(overrides)
    return MediaItem.from_json(data)
