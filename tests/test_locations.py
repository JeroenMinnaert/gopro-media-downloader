"""locations.py: one rule -- GOPRO_DL_HOME set means use it, unset means the
real OS locations. No import-time resolution, so nothing here needs a
module reload.

Tests `_resolve_root()` directly rather than `AppDirs.resolve()` -- the
whole test suite monkeypatches `AppDirs.resolve` wholesale for isolation
(see conftest.py), which would otherwise mask the real rule here too.
"""

from pathlib import Path

from gopro_dl.locations import AppDirs, _read_envrc_home, _resolve_root, default_dest


def test_gopro_dl_home_set_uses_it_directly(monkeypatch, tmp_path):
    monkeypatch.setenv("GOPRO_DL_HOME", str(tmp_path))
    assert _resolve_root() == tmp_path
    app_dirs = AppDirs(root=_resolve_root())
    assert app_dirs.token_file == tmp_path / "token"
    assert app_dirs.config_file == tmp_path / "config.env"
    assert app_dirs.browser_profile == tmp_path / "browser-profile"
    assert app_dirs.manifest_dir_for(tmp_path / "x").parent == tmp_path / "manifests"


def test_gopro_dl_home_unset_uses_the_real_os_location(monkeypatch, tmp_path):
    monkeypatch.delenv("GOPRO_DL_HOME", raising=False)
    # Away from the repo's own .envrc -- a tmp dir has no .envrc up its tree.
    monkeypatch.chdir(tmp_path)
    assert "gopro-dl" in str(_resolve_root())


# -- .envrc fallback (for when direnv isn't installed/hooked up) -----------


def test_reads_gopro_dl_home_from_the_nearest_envrc(tmp_path):
    (tmp_path / ".envrc").write_text('export GOPRO_DL_HOME="$PWD/.dev-state"\n')
    assert _read_envrc_home(tmp_path) == str(tmp_path / ".dev-state")


def test_walks_up_to_find_envrc_from_a_subdirectory(tmp_path):
    (tmp_path / ".envrc").write_text("export GOPRO_DL_HOME=/somewhere\n")
    subdir = tmp_path / "a" / "b"
    subdir.mkdir(parents=True)
    assert _read_envrc_home(subdir) == "/somewhere"


def test_a_commented_out_line_is_ignored(tmp_path):
    (tmp_path / ".envrc").write_text('# export GOPRO_DL_HOME="$PWD/.dev-state"\n')
    assert _read_envrc_home(tmp_path) is None


def test_no_envrc_anywhere_returns_none(tmp_path):
    assert _read_envrc_home(tmp_path) is None


def test_the_nearest_envrc_wins_even_without_a_matching_line(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / ".envrc").write_text("export GOPRO_DL_HOME=/should-not-be-used\n")
    (inner / ".envrc").write_text("export SOMETHING_ELSE=1\n")
    assert _read_envrc_home(inner) is None


def test_env_var_wins_over_envrc(monkeypatch, tmp_path):
    (tmp_path / ".envrc").write_text("export GOPRO_DL_HOME=/from-envrc\n")
    monkeypatch.setenv("GOPRO_DL_HOME", "/from-env-var")
    monkeypatch.chdir(tmp_path)
    assert _resolve_root() == Path("/from-env-var")


def test_default_dest_is_unrelated_to_gopro_dl_home(monkeypatch, tmp_path):
    # default_dest() only ever looks at the real Downloads folder --
    # relocating tool state (GOPRO_DL_HOME) must not silently relocate where
    # media lands too; that's what --dest/GOPRO_DEST is for.
    without_override = default_dest()
    monkeypatch.setenv("GOPRO_DL_HOME", str(tmp_path))
    assert default_dest() == without_override
    assert tmp_path not in default_dest().parents
