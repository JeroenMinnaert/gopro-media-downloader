"""Backfilling origin ETags for files downloaded before content verification."""

import httpx
import respx

from conftest import load_fixture, make_item
from gopro_dl.api import API_HOST
from gopro_dl.backfill import backfill_etags

ETAG = "cf5102a24540a33c1c1b8a771abf8b4e-39"


def seed_done(manifest, media_id="aaa111", filename="GX010001.MP4", size=1000, chapters=1):
    item = make_item(media_id, filename=filename, file_size=size)
    manifest.upsert_item(item, "2023-07-15")
    for n in range(1, chapters + 1):
        manifest.upsert_file(media_id, n, filename, f"2023-07-15/{n}_{filename}", size)
        manifest.mark_done(manifest.get_file(media_id, n)["id"], size)
    return item


@respx.mock
def test_backfill_records_the_origin_etag(manifest, client):
    seed_done(manifest)
    respx.get(f"{API_HOST}/media/aaa111/download").mock(
        httpx.Response(200, json=load_fixture("download_single"))
    )
    respx.head(url__regex=r".*/source/default/1\.mp4.*").mock(
        httpx.Response(200, headers={"ETag": f'"{ETAG}"', "Content-Length": "1000"})
    )

    report = backfill_etags(client, manifest)

    assert report.updated == 1 and not report.failed
    row = manifest.get_file("aaa111", 1)
    assert row["checksum"] == ETAG          # stored unquoted
    assert row["checksum_algo"] == "s3-etag"


@respx.mock
def test_each_chapter_gets_its_own_etag(manifest, client):
    """One API call serves the recording; each chapter is HEADed separately."""
    seed_done(manifest, "ddd444", chapters=3)
    api = respx.get(f"{API_HOST}/media/ddd444/download").mock(
        httpx.Response(200, json=load_fixture("download_chaptered"))
    )
    for n in (1, 2, 3):
        respx.head(url__regex=rf".*/source/default/{n}\.mp4.*").mock(
            httpx.Response(200, headers={"ETag": f'"chapter{n}etag"'})
        )

    report = backfill_etags(client, manifest)

    assert report.updated == 3
    assert api.call_count == 1              # not one call per chapter
    assert [manifest.get_file("ddd444", n)["checksum"] for n in (1, 2, 3)] == [
        "chapter1etag", "chapter2etag", "chapter3etag",
    ]


@respx.mock
def test_a_size_difference_against_the_origin_is_reported(manifest, client):
    seed_done(manifest, size=1000)
    respx.get(f"{API_HOST}/media/aaa111/download").mock(
        httpx.Response(200, json=load_fixture("download_single"))
    )
    respx.head(url__regex=r".*/source/.*").mock(
        httpx.Response(200, headers={"ETag": '"abc"', "Content-Length": "999"})
    )

    report = backfill_etags(client, manifest)
    assert report.size_mismatches == [("2023-07-15/1_GX010001.MP4", 1000, 999)]


@respx.mock
def test_files_that_already_have_a_checksum_are_left_alone(manifest, client):
    seed_done(manifest)
    manifest.set_checksum(manifest.get_file("aaa111", 1)["id"], "already-known")
    assert manifest.files_needing_checksum() == []

    report = backfill_etags(client, manifest)
    assert report.updated == 0
    assert manifest.get_file("aaa111", 1)["checksum"] == "already-known"


@respx.mock
def test_a_missing_etag_is_reported_not_silently_ignored(manifest, client):
    seed_done(manifest)
    respx.get(f"{API_HOST}/media/aaa111/download").mock(
        httpx.Response(200, json=load_fixture("download_single"))
    )
    respx.head(url__regex=r".*/source/.*").mock(httpx.Response(200))  # no ETag header

    report = backfill_etags(client, manifest)
    assert report.updated == 0
    assert report.failed and "no ETag" in report.failed[0][1]
