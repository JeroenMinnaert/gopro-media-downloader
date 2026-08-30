import json
import os
import threading
from pathlib import Path

import pytest

from gopro_dl.api import GoProClient
from gopro_dl.auth import AuthGate, TokenProvider
from gopro_dl.circuit import CircuitBreaker
from gopro_dl.manifest import Manifest
from gopro_dl.models import MediaItem

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    """Never let a real configuration steer the tests.

    A developer's `.env` or exported GOPRO_* variables would otherwise redirect
    the destination and -- far worse -- the manifest, so tests would write their
    fixtures into a real library's database.
    """
    for key in list(os.environ):
        if key.startswith("GOPRO_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("gopro_dl.config.load_dotenv", lambda *a, **k: None)


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
