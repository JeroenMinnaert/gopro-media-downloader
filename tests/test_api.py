"""Pagination, source-variation selection, retry/backoff and auth handling."""

import httpx
import pytest
import respx

from conftest import load_fixture, make_item
from gopro_dl.api import API_HOST, ApiError, AuthExpired
from gopro_dl.models import DEFAULT_TYPES, parse_download_response


@respx.mock
def test_pagination_walks_every_page(client):
    route = respx.get(f"{API_HOST}/media/search")
    route.side_effect = [
        httpx.Response(200, json=load_fixture("search_page1")),
        httpx.Response(200, json=load_fixture("search_page2")),
    ]
    items = list(client.iter_media(types=DEFAULT_TYPES))
    assert [i.id for i in items] == ["aaa111", "bbb222", "ccc333", "ddd444"]
    assert route.call_count == 2
    # per_page must be the large page size, not the website's default
    assert "per_page=100" in str(route.calls[0].request.url)


@respx.mock
def test_multi_clip_edits_are_excluded_from_the_request(client):
    route = respx.get(f"{API_HOST}/media/search").mock(
        httpx.Response(200, json=load_fixture("search_page1"))
    )
    list(client.iter_media(types=DEFAULT_TYPES))
    assert "MultiClipEdit" not in str(route.calls[0].request.url)


@respx.mock
def test_bearer_token_is_sent(client):
    route = respx.get(f"{API_HOST}/media/user").mock(httpx.Response(200, json={"id": "u1"}))
    client.validate_token()
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-token"


@respx.mock
def test_cookie_fallback_when_the_header_is_refused(client):
    def responder(request):
        if "Authorization" in request.headers:
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"id": "u1"})

    respx.get(f"{API_HOST}/media/user").mock(side_effect=responder)
    assert client.validate_token() == {"id": "u1"}
    assert client.auth_mode == "cookie"


@respx.mock
def test_dead_token_raises_auth_expired_not_a_file_failure(client):
    respx.get(f"{API_HOST}/media/aaa/download").mock(httpx.Response(401, json={}))
    with pytest.raises(AuthExpired):
        client.get_download("aaa")


@respx.mock
def test_429_is_retried_then_succeeds(client):
    route = respx.get(f"{API_HOST}/media/user")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(503),
        httpx.Response(200, json={"id": "u1"}),
    ]
    assert client.validate_token() == {"id": "u1"}
    assert route.call_count == 3


@respx.mock
def test_persistent_5xx_gives_up_after_the_attempt_budget(client):
    respx.get(f"{API_HOST}/media/aaa/download").mock(httpx.Response(503))
    with pytest.raises(ApiError):
        client.get_download("aaa")


def test_source_variation_is_chosen_over_every_transcode():
    item = make_item(file_size=1048576)
    files, skip = parse_download_response(load_fixture("download_single"), item)
    assert skip is None
    assert len(files) == 1
    assert "/source/" in files[0].url
    assert "proxy" not in files[0].url and "mp4_low" not in files[0].url
    # variations carry no size, so the listing's file_size is the expectation
    assert files[0].size == 1048576


def test_embedded_files_is_ignored_because_it_points_at_the_proxy():
    """Regression: `_embedded.files` holds high_res_proxy_mp4 for videos.

    Trusting it downloads a 1080p transcode at roughly half the bytes of the
    original -- silently, since it looks like a complete file.
    """
    data = load_fixture("download_single")
    assert "proxy" in data["_embedded"]["files"][0]["url"], "fixture must mirror the real API"
    files, _ = parse_download_response(data, make_item())
    assert all("proxy" not in f.url for f in files)


def test_every_source_chapter_is_returned_not_just_the_first():
    """Regression: chapters share the label "source" and differ only by URL.

    Taking the first match silently dropped chapters 2..N of long recordings.
    """
    item = make_item(filename="GX010001.MP4")
    files, skip = parse_download_response(load_fixture("download_chaptered"), item)
    assert skip is None
    assert [f.item_number for f in files] == [1, 2, 3]
    # chapter names are reconstructed the way the camera would have written them
    assert [f.filename for f in files] == ["GX010001.MP4", "GX020001.MP4", "GX030001.MP4"]
    assert [f.url.rsplit("/", 1)[1] for f in files] == ["1.mp4", "2.mp4", "3.mp4"]
    # per-chapter sizes are unknown from the API; they are learned while downloading
    assert all(f.size is None for f in files)


def test_photo_source_is_found_even_though_files_matches_it():
    files, skip = parse_download_response(load_fixture("download_photo"), make_item(file_size=854047))
    assert skip is None and len(files) == 1
    assert files[0].url.endswith("1.jpg") and files[0].size == 854047


def test_missing_source_variation_is_a_skip_not_a_crash():
    files, skip = parse_download_response(load_fixture("download_no_source"), make_item())
    assert files == [] and skip == "no_source_variation"
