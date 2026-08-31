"""Network-mount detection and the manifest/log auto-redirect it triggers.

SMB/NFS silently corrupt SQLite's WAL journal, so a --dest on a network mount
should get its manifest.db and logs redirected to a local, per-user location
instead -- unless the user already picked one with --manifest-dir.
"""

import subprocess
from types import SimpleNamespace

import httpx
import respx

import gopro_dl.cli as cli_module
from gopro_dl.api import API_HOST
from gopro_dl.config import Config, apply_network_manifest_redirect
from gopro_dl.locations import AppDirs
from gopro_dl.preflight import is_network_filesystem


def _fake_run(stdout: str):
    """A stand-in for `subprocess.run(...)`'s return value, exposing just
    the `.stdout` attribute the preflight helpers read."""
    return lambda *a, **k: SimpleNamespace(stdout=stdout)


# -- preflight.is_network_filesystem ---------------------------------------


def test_detects_smbfs_on_macos(monkeypatch, tmp_path):
    from gopro_dl import preflight

    monkeypatch.setattr(preflight.sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess, "run",
        _fake_run(f"//nas/GoPro on {tmp_path} (smbfs, nodev, nosuid, mounted by you)\n"),
    )
    assert is_network_filesystem(tmp_path) is True


def test_local_disk_is_not_flagged_on_macos(monkeypatch, tmp_path):
    from gopro_dl import preflight

    monkeypatch.setattr(preflight.sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess, "run",
        _fake_run(f"/dev/disk3s1 on {tmp_path} (apfs, local, journaled)\n"),
    )
    assert is_network_filesystem(tmp_path) is False


def test_macos_matches_the_longest_prefix_mount_point(monkeypatch, tmp_path):
    """A subdirectory of a network share must still resolve to that share's type."""
    from gopro_dl import preflight

    subdir = tmp_path / "backup"
    subdir.mkdir()
    monkeypatch.setattr(preflight.sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess, "run",
        _fake_run(f"/dev/disk1 on / (apfs, local)\n//nas/x on {tmp_path} (smbfs, nodev)\n"),
    )
    assert is_network_filesystem(subdir) is True


def test_macos_root_mount_matches_without_a_double_slash_bug(monkeypatch, tmp_path):
    """Regression: the root mount point is literally "/", so a naive
    `path.startswith(mount_point + "/")` prefix check turns into checking for
    a "//" prefix, which no real path has -- silently failing detection for
    anything not under a more specific mount."""
    from gopro_dl import preflight

    monkeypatch.setattr(preflight.sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess, "run",
        _fake_run("/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"),
    )
    assert is_network_filesystem(tmp_path) is False
    assert preflight._fs_type_macos(tmp_path) == "apfs"


def test_detects_cifs_on_linux(monkeypatch, tmp_path):
    from gopro_dl import preflight

    monkeypatch.setattr(preflight.sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", _fake_run("FSTYPE\ncifs\n"))
    assert is_network_filesystem(tmp_path) is True


def test_local_disk_is_not_flagged_on_linux(monkeypatch, tmp_path):
    from gopro_dl import preflight

    monkeypatch.setattr(preflight.sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", _fake_run("FSTYPE\next4\n"))
    assert is_network_filesystem(tmp_path) is False


def test_detection_failure_fails_open_to_false(monkeypatch, tmp_path):
    from gopro_dl import preflight

    monkeypatch.setattr(preflight.sys, "platform", "linux")

    def raise_oserror(*a, **k):
        raise OSError("df: command not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)
    assert is_network_filesystem(tmp_path) is False


def test_walks_up_to_the_nearest_existing_ancestor(monkeypatch, tmp_path):
    """A --dest that doesn't exist yet must be checked via its parent."""
    from gopro_dl import preflight

    not_yet_created = tmp_path / "GoPro" / "downloads"
    monkeypatch.setattr(preflight.sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", _fake_run(f"//nas/x on {tmp_path} (smbfs, nodev)\n"))
    assert is_network_filesystem(not_yet_created) is True


# -- AppDirs.manifest_dir_for ------------------------------------------------


def test_manifest_dir_for_is_deterministic(tmp_path):
    app_dirs = AppDirs(root=tmp_path / "app")
    dest = tmp_path / "GoPro"
    dest.mkdir()
    first = app_dirs.manifest_dir_for(dest)
    second = app_dirs.manifest_dir_for(dest)
    assert first == second
    assert first.parent == app_dirs.root / "manifests"
    assert first.name.startswith("GoPro-")


def test_manifest_dir_for_disambiguates_same_named_destinations(tmp_path):
    app_dirs = AppDirs(root=tmp_path / "app")
    a = tmp_path / "one" / "GoPro"
    b = tmp_path / "two" / "GoPro"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert app_dirs.manifest_dir_for(a) != app_dirs.manifest_dir_for(b)


# -- config.apply_network_manifest_redirect ---------------------------------


def test_redirects_when_dest_is_a_network_mount(tmp_path, monkeypatch):
    monkeypatch.setattr("gopro_dl.config.is_network_filesystem", lambda dest: True)
    app_dirs = AppDirs(root=tmp_path / "app")
    config = Config(dest=tmp_path / "GoPro", app_dirs=app_dirs)
    notice = apply_network_manifest_redirect(config)
    assert notice is not None
    assert "network mount" in notice
    assert config.manifest_dir == app_dirs.manifest_dir_for(tmp_path / "GoPro")


def test_does_not_redirect_a_local_destination(tmp_path, monkeypatch):
    monkeypatch.setattr("gopro_dl.config.is_network_filesystem", lambda dest: False)
    config = Config(dest=tmp_path / "downloads", app_dirs=AppDirs(root=tmp_path / "app"))
    notice = apply_network_manifest_redirect(config)
    assert notice is None
    assert config.manifest_dir is None


def test_an_explicit_manifest_dir_always_wins(tmp_path, monkeypatch):
    monkeypatch.setattr("gopro_dl.config.is_network_filesystem", lambda dest: True)
    chosen = tmp_path / "my-local-state"
    config = Config(
        dest=tmp_path / "GoPro", app_dirs=AppDirs(root=tmp_path / "app"), manifest_dir=chosen
    )
    notice = apply_network_manifest_redirect(config)
    assert notice is None
    assert config.manifest_dir == chosen


def test_an_already_colocated_manifest_is_never_redirected(tmp_path, monkeypatch):
    """Once a manifest exists at the normal colocated path, never split it by
    redirecting elsewhere -- and skip the mount/df subprocess call entirely."""
    dest = tmp_path / "GoPro"
    manifest_dir = dest / ".gopro-dl"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.db").write_text("")

    def fail_if_called(dest):
        raise AssertionError("is_network_filesystem must not be called")

    monkeypatch.setattr("gopro_dl.config.is_network_filesystem", fail_if_called)
    config = Config(dest=dest, app_dirs=AppDirs(root=tmp_path / "app"))
    notice = apply_network_manifest_redirect(config)
    assert notice is None
    assert config.manifest_dir is None


# -- cli.py wiring: only manifest-touching commands check at all -----------


def test_redirect_check_runs_for_a_manifest_command(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli_module, "apply_network_manifest_redirect", lambda config: calls.append(1))
    cli_module.main(["status", "--dest", str(tmp_path / "dest")])
    assert calls == [1]


def test_redirect_check_is_skipped_for_setup_and_token(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli_module, "apply_network_manifest_redirect", lambda config: calls.append(1))
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda *a, **k: None)
    monkeypatch.setattr(cli_module.console, "input", lambda *a, **k: "")

    with respx.mock:
        respx.get(f"{API_HOST}/media/user").mock(return_value=httpx.Response(401))
        cli_module.main(["token", "--token", "whatever"])
        cli_module.main(["setup", "--no-browser", "--dest", str(tmp_path / "dest")])

    assert calls == []
