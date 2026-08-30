"""locations.py: one rule -- GOPRO_DL_HOME set means use it, unset means the
real OS locations. No import-time resolution, so nothing here needs a
module reload.

Tests `_resolve_root()` directly rather than `AppDirs.resolve()` -- the
whole test suite monkeypatches `AppDirs.resolve` wholesale for isolation
(see conftest.py), which would otherwise mask the real rule here too.
"""

from gopro_dl.locations import AppDirs, _resolve_root, default_dest


def test_gopro_dl_home_set_uses_it_directly(monkeypatch, tmp_path):
    monkeypatch.setenv("GOPRO_DL_HOME", str(tmp_path))
    assert _resolve_root() == tmp_path
    app_dirs = AppDirs(root=_resolve_root())
    assert app_dirs.token_file == tmp_path / "token"
    assert app_dirs.config_file == tmp_path / "config.env"
    assert app_dirs.browser_profile == tmp_path / "browser-profile"
    assert app_dirs.manifest_dir_for(tmp_path / "x").parent == tmp_path / "manifests"


def test_gopro_dl_home_unset_uses_the_real_os_location(monkeypatch):
    monkeypatch.delenv("GOPRO_DL_HOME", raising=False)
    assert "gopro-dl" in str(_resolve_root())


def test_default_dest_is_unrelated_to_gopro_dl_home(monkeypatch, tmp_path):
    # default_dest() only ever looks at the real Downloads folder --
    # relocating tool state (GOPRO_DL_HOME) must not silently relocate where
    # media lands too; that's what --dest/GOPRO_DEST is for.
    without_override = default_dest()
    monkeypatch.setenv("GOPRO_DL_HOME", str(tmp_path))
    assert default_dest() == without_override
    assert tmp_path not in default_dest().parents
