"""appdirs.py: dev mode is auto-detected from an editable install, and
GOPRO_DL_HOME can still override the location explicitly either way.
"""

import importlib

import gopro_dl.appdirs as appdirs_module


def _reload():
    importlib.reload(appdirs_module)


def test_gopro_dl_home_overrides_everything_explicitly(tmp_path, monkeypatch):
    monkeypatch.setenv("GOPRO_DL_HOME", str(tmp_path))
    _reload()
    try:
        assert tmp_path == appdirs_module.CONFIG_DIR
        assert tmp_path == appdirs_module.DATA_DIR
        assert tmp_path / "token" == appdirs_module.DEFAULT_TOKEN_FILE
        assert tmp_path / "config.env" == appdirs_module.DEFAULT_ENV_FILE
        assert appdirs_module.default_dest() == tmp_path / "downloads"
        assert appdirs_module.manifest_dir_for(tmp_path / "x").parent == tmp_path / "manifests"
    finally:
        monkeypatch.delenv("GOPRO_DL_HOME", raising=False)
        _reload()


def test_source_checkout_root_finds_the_real_repo_when_running_from_it():
    # This test suite itself only ever runs from an editable install.
    root = appdirs_module._source_checkout_root()
    assert root is not None
    assert (root / "pyproject.toml").is_file()
    assert (root / "src" / "gopro_dl").is_dir()


def test_source_checkout_root_returns_none_for_a_real_install_layout(tmp_path, monkeypatch):
    # Simulates site-packages: no sibling pyproject.toml/src/gopro_dl anywhere
    # up the tree, unlike an editable install.
    fake_site_packages = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "gopro_dl"
    fake_site_packages.mkdir(parents=True)
    monkeypatch.setattr(appdirs_module, "__file__", str(fake_site_packages / "appdirs.py"))
    assert appdirs_module._source_checkout_root() is None


def test_without_gopro_dl_home_auto_detects_the_checkout_and_uses_dev_state(monkeypatch):
    # No explicit override, but this test itself runs from the real editable
    # checkout, so detection should kick in automatically.
    monkeypatch.delenv("GOPRO_DL_HOME", raising=False)
    _reload()
    try:
        root = appdirs_module._source_checkout_root()
        assert root is not None
        assert root / ".dev-state" == appdirs_module.CONFIG_DIR
        assert root / ".dev-state" == appdirs_module.DATA_DIR
    finally:
        _reload()


def test_resolve_dev_home_falls_back_to_none_without_a_checkout_or_override(monkeypatch):
    monkeypatch.delenv("GOPRO_DL_HOME", raising=False)
    monkeypatch.setattr(appdirs_module, "_source_checkout_root", lambda: None)
    assert appdirs_module._resolve_dev_home() is None


def test_resolve_dev_home_prefers_the_explicit_override_over_a_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("GOPRO_DL_HOME", str(tmp_path))
    assert appdirs_module._resolve_dev_home() == str(tmp_path)


def test_force_real_skips_both_the_override_and_checkout_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("GOPRO_DL_FORCE_REAL", "1")
    monkeypatch.setenv("GOPRO_DL_HOME", str(tmp_path))  # must still be ignored
    assert appdirs_module._resolve_dev_home() is None


def test_force_real_makes_config_dir_use_the_real_os_location(monkeypatch):
    monkeypatch.setenv("GOPRO_DL_FORCE_REAL", "1")
    _reload()
    try:
        assert appdirs_module._source_checkout_root() / ".dev-state" != appdirs_module.CONFIG_DIR
        assert "gopro-dl" in str(appdirs_module.CONFIG_DIR)
    finally:
        monkeypatch.delenv("GOPRO_DL_FORCE_REAL", raising=False)
        _reload()
