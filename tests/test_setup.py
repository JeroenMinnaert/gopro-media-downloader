"""gopro-dl setup: cached-session/browser-login fallbacks and the write guards."""

import os

import httpx
import respx

import gopro_dl.cli as cli_module
from gopro_dl.api import API_HOST
from gopro_dl.cli import _detect_timezone, main


def _run_setup(tmp_path, *extra_args, token_file=None, dest=None, with_timezone=True, with_dest=True):
    args = ["setup", "--token-file", str(token_file if token_file is not None else tmp_path / "tok")]
    if with_dest:
        args += ["--dest", str(dest if dest is not None else tmp_path / "dl")]
    if with_timezone:
        args += ["--timezone", "Europe/Brussels"]
    return main([*args, *extra_args])


def _validate_ok(email="jane@example.com"):
    respx.get(f"{API_HOST}/media/user").mock(return_value=httpx.Response(200, json={"email": email}))


def _validate_only(good_token):
    respx.get(f"{API_HOST}/media/user").mock(
        side_effect=lambda r: httpx.Response(200, json={"email": "jane@example.com"})
        if good_token in r.headers.get("Authorization", "")
        else httpx.Response(401)
    )


def test_uses_a_cached_browser_session_without_prompting(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: "cached-token")
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_ok()
        code = _run_setup(tmp_path)
    assert code == 0
    assert (tmp_path / "tok").read_text().strip() == "cached-token"
    assert oct((tmp_path / "tok").stat().st_mode)[-3:] == "600"
    env = (tmp_path / ".env").read_text()
    assert "GOPRO_TOKEN_FILE=" in env
    assert "GOPRO_DEST=" in env


def test_falls_back_to_browser_login_when_no_cached_session(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: None)
    monkeypatch.setattr(cli_module, "login_via_browser", lambda console: "logged-in-token")
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_ok()
        code = _run_setup(tmp_path)
    assert code == 0
    assert (tmp_path / "tok").read_text().strip() == "logged-in-token"


def test_expired_cached_session_falls_back_to_browser_login(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: "stale-token")
    monkeypatch.setattr(cli_module, "login_via_browser", lambda console: "fresh-token")
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_only("fresh-token")
        code = _run_setup(tmp_path)
    assert code == 0
    assert (tmp_path / "tok").read_text().strip() == "fresh-token"


def test_falls_back_to_manual_paste_when_browser_login_is_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: None)
    monkeypatch.setattr(cli_module, "login_via_browser", lambda console: None)  # window closed / timed out
    monkeypatch.setattr(cli_module.console, "input", lambda *a, **k: "pasted-token")
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_ok()
        code = _run_setup(tmp_path)
    assert code == 0
    assert (tmp_path / "tok").read_text().strip() == "pasted-token"


def test_browser_not_installed_falls_back_to_manual_paste(tmp_path, monkeypatch):
    # login_via_browser() never raises BrowserNotInstalled -- it handles that
    # itself and returns None, same as any other failed/cancelled login.
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: None)
    monkeypatch.setattr(cli_module, "login_via_browser", lambda console: None)
    monkeypatch.setattr(cli_module.console, "input", lambda *a, **k: "pasted-token")
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_ok()
        code = _run_setup(tmp_path)
    assert code == 0
    assert (tmp_path / "tok").read_text().strip() == "pasted-token"


def test_no_browser_flag_skips_cache_and_login_entirely(tmp_path, monkeypatch):
    def fail_if_called(*a, **k):
        raise AssertionError("browser paths must not run with --no-browser")

    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", fail_if_called)
    monkeypatch.setattr(cli_module, "login_via_browser", fail_if_called)
    monkeypatch.setattr(cli_module.console, "input", lambda *a, **k: "typed-token")
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_ok()
        code = _run_setup(tmp_path, "--no-browser")
    assert code == 0
    assert (tmp_path / "tok").read_text().strip() == "typed-token"


def test_does_not_overwrite_an_existing_token_file_without_confirmation(tmp_path, monkeypatch):
    token_file = tmp_path / "tok"
    token_file.write_text("original-token")
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: "cached-token")
    monkeypatch.setattr(cli_module.console, "input", lambda *a, **k: "n")  # decline overwrite
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_ok()
        code = _run_setup(tmp_path, token_file=token_file)
    assert code == 0
    assert token_file.read_text() == "original-token"


def test_force_overwrites_existing_token_file_and_env(tmp_path, monkeypatch):
    token_file = tmp_path / "tok"
    token_file.write_text("original-token")
    (tmp_path / ".env").write_text("STALE=1\n")
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: "cached-token")
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_ok()
        code = _run_setup(tmp_path, "--force", token_file=token_file)
    assert code == 0
    assert token_file.read_text().strip() == "cached-token"
    assert "STALE" not in (tmp_path / ".env").read_text()


def test_cancelling_the_manual_paste_prompt_aborts_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: None)
    monkeypatch.setattr(cli_module, "login_via_browser", lambda console: None)

    def raise_eof(*a, **k):
        raise EOFError

    monkeypatch.setattr(cli_module.console, "input", raise_eof)
    monkeypatch.chdir(tmp_path)
    code = _run_setup(tmp_path)
    assert code == 130
    assert not (tmp_path / "tok").exists()


def test_an_empty_manual_paste_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: None)
    monkeypatch.setattr(cli_module, "login_via_browser", lambda console: None)
    monkeypatch.setattr(cli_module.console, "input", lambda *a, **k: "")
    monkeypatch.chdir(tmp_path)
    code = _run_setup(tmp_path)
    assert code == 1
    assert not (tmp_path / "tok").exists()


def test_leaves_an_existing_env_file_untouched_without_force(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("EXISTING=1\n")
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: "cached-token")
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_ok()
        code = _run_setup(tmp_path)
    assert code == 0
    assert (tmp_path / ".env").read_text() == "EXISTING=1\n"


def test_cancelling_the_overwrite_prompt_leaves_the_token_file_untouched(tmp_path, monkeypatch):
    token_file = tmp_path / "tok"
    token_file.write_text("original-token")
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: "cached-token")

    def raise_eof(*a, **k):
        raise EOFError

    monkeypatch.setattr(cli_module.console, "input", raise_eof)
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_ok()
        code = _run_setup(tmp_path, token_file=token_file)
    assert code == 0
    assert token_file.read_text() == "original-token"


def test_detected_timezone_is_used_without_prompting(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: "cached-token")
    monkeypatch.setattr(cli_module, "_detect_timezone", lambda: "Europe/Brussels")

    def fail_if_asked(*a, **k):
        raise AssertionError("should not prompt when detection succeeds")

    monkeypatch.setattr(cli_module.console, "input", fail_if_asked)
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_ok()
        code = _run_setup(tmp_path, with_timezone=False)
    assert code == 0
    assert "GOPRO_TIMEZONE=Europe/Brussels" in (tmp_path / ".env").read_text()


def test_falls_back_to_the_prompt_when_detection_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: "cached-token")
    monkeypatch.setattr(cli_module, "_detect_timezone", lambda: None)
    monkeypatch.setattr(cli_module.console, "input", lambda *a, **k: "Europe/Paris")
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_ok()
        code = _run_setup(tmp_path, with_timezone=False)
    assert code == 0
    assert "GOPRO_TIMEZONE=Europe/Paris" in (tmp_path / ".env").read_text()


def test_cancelling_the_timezone_prompt_just_skips_it(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: "cached-token")
    monkeypatch.setattr(cli_module, "_detect_timezone", lambda: None)

    def raise_keyboard_interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module.console, "input", raise_keyboard_interrupt)
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_ok()
        code = _run_setup(tmp_path, with_timezone=False)
    assert code == 0
    assert "GOPRO_TIMEZONE" not in (tmp_path / ".env").read_text()


def test_an_invalid_typed_timezone_is_ignored_with_a_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: "cached-token")
    monkeypatch.setattr(cli_module, "_detect_timezone", lambda: None)
    monkeypatch.setattr(cli_module.console, "input", lambda *a, **k: "Not/A/Zone")
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_ok()
        code = _run_setup(tmp_path, with_timezone=False)
    assert code == 0
    assert "GOPRO_TIMEZONE" not in (tmp_path / ".env").read_text()


def test_rejects_an_unvalidatable_token_without_writing_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: "bad-token")
    monkeypatch.setattr(cli_module, "login_via_browser", lambda console: "still-bad")
    monkeypatch.setattr(cli_module.console, "input", lambda *a, **k: "also-bad")
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        respx.get(f"{API_HOST}/media/user").mock(return_value=httpx.Response(401))
        code = _run_setup(tmp_path)
    assert code == 1
    assert not (tmp_path / "tok").exists()
    assert not (tmp_path / ".env").exists()


# -- cli._detect_timezone ----------------------------------------------------


def test_detect_timezone_reads_the_macos_style_localtime_symlink(monkeypatch):
    monkeypatch.setattr(os, "readlink", lambda p: "/var/db/timezone/zoneinfo/Europe/Brussels")
    assert _detect_timezone() == "Europe/Brussels"


def test_detect_timezone_reads_the_linux_style_localtime_symlink(monkeypatch):
    monkeypatch.setattr(os, "readlink", lambda p: "/usr/share/zoneinfo/America/New_York")
    assert _detect_timezone() == "America/New_York"


def test_detect_timezone_returns_none_without_a_localtime_symlink(monkeypatch):
    def raise_oserror(p):
        raise OSError("no such file")

    monkeypatch.setattr(os, "readlink", raise_oserror)
    assert _detect_timezone() is None


def test_detect_timezone_returns_none_for_a_target_with_no_zoneinfo_marker(monkeypatch):
    monkeypatch.setattr(os, "readlink", lambda p: "/etc/some-other-file")
    assert _detect_timezone() is None


def test_detect_timezone_returns_none_for_an_invalid_zone_name(monkeypatch):
    monkeypatch.setattr(os, "readlink", lambda p: "/usr/share/zoneinfo/Not/A/Real/Zone")
    assert _detect_timezone() is None


# -- cmd_setup: the destination prompt ---------------------------------------


def test_dest_flag_skips_the_destination_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: "cached-token")

    def fail_if_asked(*a, **k):
        raise AssertionError("must not prompt when --dest and --timezone are both given")

    monkeypatch.setattr(cli_module.console, "input", fail_if_asked)
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_ok()
        code = _run_setup(tmp_path)
    assert code == 0


def test_prompts_for_a_destination_when_not_passed_via_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: "cached-token")
    custom_dest = tmp_path / "my-media"
    monkeypatch.setattr(cli_module.console, "input", lambda *a, **k: str(custom_dest))
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_ok()
        code = _run_setup(tmp_path, with_dest=False)
    assert code == 0
    assert custom_dest.is_dir()
    assert f"GOPRO_DEST={custom_dest}" in (tmp_path / ".env").read_text()


def test_pressing_enter_accepts_the_default_destination(tmp_path, monkeypatch):
    # default_dest() resolves to the real ~/Downloads/GoPro -- redirect it so
    # accepting the default in this test never touches the real home directory.
    fake_default = tmp_path / "fake-default-dest"
    monkeypatch.setattr("gopro_dl.config.default_dest", lambda: fake_default)
    monkeypatch.setattr(cli_module, "fetch_cached_browser_token", lambda: "cached-token")
    monkeypatch.setattr(cli_module.console, "input", lambda *a, **k: "")
    monkeypatch.chdir(tmp_path)
    with respx.mock:
        _validate_ok()
        code = _run_setup(tmp_path, with_dest=False)
    assert code == 0
    assert fake_default.is_dir()
    assert f"GOPRO_DEST={fake_default}" in (tmp_path / ".env").read_text()
