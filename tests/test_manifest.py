"""Manifest sync semantics: idempotency, filters, recovery, accounting."""

from conftest import make_item

from gopro_dl.manifest import Manifest


def add(manifest, media_id, folder="2023-07-15", size=1000, **kw):
    item = make_item(media_id, **kw)
    manifest.upsert_item(item, folder)
    return item


def test_resync_is_idempotent_and_never_resurrects_finished_work(manifest):
    add(manifest, "aaa111")
    manifest.upsert_file("aaa111", 1, "GX010001.MP4", "2023-07-15/GX010001.MP4", 1000)
    manifest.mark_done(manifest.get_file("aaa111", 1)["id"], 1000)
    manifest.refresh_item_state("aaa111")

    add(manifest, "aaa111")  # a later run sees the same item again
    add(manifest, "aaa111")

    assert manifest.get_item("aaa111")["state"] == "done"
    assert manifest.pending_items() == []
    assert len(manifest.files_for("aaa111")) == 1  # no duplicate rows


def test_new_items_appear_without_disturbing_old_ones(manifest):
    add(manifest, "aaa111")
    manifest.upsert_file("aaa111", 1, "a.MP4", "2023-07-15/a.MP4", 1000)
    manifest.mark_done(manifest.get_file("aaa111", 1)["id"], 1000)
    manifest.refresh_item_state("aaa111")

    add(manifest, "bbb222", folder="2024-01-02")
    assert [r["id"] for r in manifest.pending_items()] == ["bbb222"]


def test_skipped_items_stay_out_of_the_queue(manifest):
    item = make_item("ccc333", type="MultiClipEdit", mce_type="MultiClipEdit")
    manifest.upsert_item(item, "2024-02-01", skip_reason=item.skip_reason())
    assert manifest.pending_items() == []
    assert manifest.skipped_items()[0]["skip_reason"] == "gopro_generated_edit"


def test_date_filters_and_limit(manifest):
    add(manifest, "old1", folder="2020-01-01")
    add(manifest, "mid1", folder="2023-07-15")
    add(manifest, "new1", folder="2025-06-01")

    assert [r["id"] for r in manifest.pending_items(since="2023-01-01")] == ["mid1", "new1"]
    assert [r["id"] for r in manifest.pending_items(until="2023-12-31")] == ["old1", "mid1"]
    assert [r["id"] for r in manifest.pending_items(since="2023-01-01", until="2023-12-31")] == ["mid1"]
    assert len(manifest.pending_items(limit=2)) == 2


def test_attempt_budget_stops_an_item_retrying_forever(manifest):
    add(manifest, "aaa111")
    for _ in range(4):
        manifest.claim_item("aaa111")
        manifest.mark_item_failed("aaa111", "boom")
    assert manifest.pending_items(max_attempts=4) == []
    assert len(manifest.pending_items(max_attempts=5)) == 1


def test_retry_failed_requeues(manifest):
    add(manifest, "aaa111")
    manifest.upsert_file("aaa111", 1, "a.MP4", "2023-07-15/a.MP4", 1000)
    file_id = manifest.get_file("aaa111", 1)["id"]
    manifest.claim_file(file_id)
    manifest.mark_failed(file_id, "boom")
    manifest.refresh_item_state("aaa111")

    items, files = manifest.reset_failed()
    assert files == 1
    assert manifest.get_file("aaa111", 1)["state"] == "pending"
    assert manifest.get_file("aaa111", 1)["attempts"] == 0
    assert [r["id"] for r in manifest.pending_items()] == ["aaa111"]


def test_crash_leftovers_are_recovered(manifest):
    add(manifest, "aaa111")
    manifest.upsert_file("aaa111", 1, "a.MP4", "2023-07-15/a.MP4", 1000)
    manifest.claim_item("aaa111")
    manifest.claim_file(manifest.get_file("aaa111", 1)["id"])
    # process dies here: item='resolving', file='downloading'

    items, files = manifest.reset_stale()
    assert (items, files) == (1, 1)
    assert manifest.get_item("aaa111")["state"] == "pending"
    assert manifest.get_file("aaa111", 1)["state"] == "pending"


def test_target_paths_are_unique_and_immutable(manifest):
    add(manifest, "aaa111")
    manifest.upsert_file("aaa111", 1, "GX010001.MP4", "2023-07-15/GX010001.MP4", 1000)
    assert manifest.path_taken("2023-07-15/GX010001.MP4")
    assert not manifest.path_taken("2023-07-15/GX010001.MP4", exclude_media_id="aaa111")

    # a size refresh must not move the file
    manifest.upsert_file("aaa111", 1, "GX010001.MP4", "2023-07-15/SOMEWHERE_ELSE.MP4", 2000)
    row = manifest.get_file("aaa111", 1)
    assert row["target_path"] == "2023-07-15/GX010001.MP4"
    assert row["expected_size"] == 1000 or row["expected_size"] == 2000


def test_remaining_bytes_mixes_resolved_and_unresolved(manifest):
    add(manifest, "unresolved", size=5000)  # only the listing hint is known
    add(manifest, "resolved")
    manifest.upsert_file("resolved", 1, "a.MP4", "2023-07-15/a.MP4", 300)
    manifest.upsert_file("resolved", 2, "b.MP4", "2023-07-15/b.MP4", 700)
    manifest.refresh_item_state("resolved")

    assert manifest.remaining_bytes() == 1000 + 1000  # listing hint + two chapters
    manifest.mark_done(manifest.get_file("resolved", 1)["id"], 300)
    assert manifest.remaining_bytes() == 1000 + 700


def test_manifest_survives_reopen(tmp_path):
    path = tmp_path / "m.db"
    with Manifest(path) as first:
        first.upsert_item(make_item("aaa111"), "2023-07-15")
    with Manifest(path) as second:
        assert second.get_item("aaa111") is not None
        assert second.quick_check()


def test_ambient_configuration_cannot_redirect_a_run(tmp_path, monkeypatch):
    """Regression: a stray .env/GOPRO_MANIFEST_DIR once pointed tests at a real
    manifest, writing fixture rows into a live library."""
    from gopro_dl.config import load_config
    from gopro_dl.locations import AppDirs

    monkeypatch.setenv("GOPRO_MANIFEST_DIR", "/somewhere/real")
    monkeypatch.setenv("GOPRO_DEST", "/somewhere/real/media")

    class Args:
        dest = str(tmp_path / "dest")
        manifest_dir = str(tmp_path / "state")
        token = "t"

    config = load_config(Args(), AppDirs(root=tmp_path / "app"))
    # explicit arguments must win over the ambient environment
    assert config.manifest_path == tmp_path / "state" / "manifest.db"
    assert config.dest == tmp_path / "dest"


def test_free_space_survives_the_smbfs_32bit_truncation(monkeypatch, tmp_path):
    """Regression: macOS smbfs wraps statvfs block counts at 2**32.

    A 7.2 TiB SMB share reported only 1.2 TiB free through shutil, which made
    pre-flight refuse a run on a destination with 5.2 TiB available.
    """
    import shutil as _shutil
    import subprocess

    from gopro_dl.preflight import check_disk_space, free_space

    TRUE_FREE_KIB = 5_601_011_852                     # what df reports
    WRAPPED = TRUE_FREE_KIB - 2**32                   # what statvfs returns

    monkeypatch.setattr(
        _shutil, "disk_usage", lambda p: _shutil._ntuple_diskusage(0, 0, WRAPPED * 1024)
    )

    class Result:
        stdout = (
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            f"//nas/GoPro 7735264828 2134252976 {TRUE_FREE_KIB} 28% /Volumes/GoPro\n"
        )

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Result())

    assert free_space(tmp_path) == TRUE_FREE_KIB * 1024
    # a multi-terabyte library now fits, where the wrapped value would have refused it
    assert check_disk_space(tmp_path, 1432 * 1024**3).ok


def test_free_space_falls_back_to_statvfs_when_df_is_unavailable(monkeypatch, tmp_path):
    import shutil as _shutil
    import subprocess

    from gopro_dl.preflight import free_space

    monkeypatch.setattr(
        _shutil, "disk_usage", lambda p: _shutil._ntuple_diskusage(0, 0, 12345)
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no df")))
    assert free_space(tmp_path) == 12345
