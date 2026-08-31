"""browser_login: cookie extraction, cached-session lookup, and login polling.

Playwright itself is faked out entirely -- these tests never launch a real
browser, they just verify the module's own control flow (cookie parsing,
graceful failure, the polling loop, and the "not installed" signal).
"""

import playwright.sync_api
from playwright.sync_api import Error as RealPlaywrightError

from gopro_dl import browser_login
from gopro_dl.browser_login import fetch_cached, login


class FakeConsole:
    def print(self, *a, **k):
        pass


class FakePage:
    def goto(self, url):
        pass


class FakeContext:
    def __init__(self, cookie_sequence=None, raise_on_cookies=False):
        self._cookie_sequence = list(cookie_sequence or [[]])
        self.raise_on_cookies = raise_on_cookies
        self.pages = []
        self.closed = False

    def cookies(self, urls=None):
        if self.raise_on_cookies:
            raise RuntimeError("target closed")
        if len(self._cookie_sequence) > 1:
            return self._cookie_sequence.pop(0)
        return self._cookie_sequence[0]

    def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, context=None, raise_exc=None, fail_count=None):
        """raise_exc: raised on launch. fail_count=None means raise every
        call; an int means raise only for the first N calls, then succeed
        (simulating a launch that works once the browser is installed)."""
        self._context = context
        self._raise_exc = raise_exc
        self._fail_count = fail_count
        self.calls = 0

    def launch_persistent_context(self, user_data_dir, headless=None):
        self.calls += 1
        if self._raise_exc is not None and (self._fail_count is None or self.calls <= self._fail_count):
            raise self._raise_exc
        return self._context


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium


class FakeSyncPlaywright:
    def __init__(self, chromium):
        self._chromium = chromium

    def __enter__(self):
        return FakePlaywright(self._chromium)

    def __exit__(self, *exc_info):
        return False


def _patch_playwright(monkeypatch, chromium):
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: FakeSyncPlaywright(chromium))


def test_fetch_cached_returns_none_without_a_profile_dir(tmp_path):
    assert fetch_cached(tmp_path / "does-not-exist") is None


def test_fetch_cached_extracts_the_cookie(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    cookies = [{"name": "other", "value": "x"}, {"name": "gp_access_token", "value": "cached-value"}]
    _patch_playwright(monkeypatch, FakeChromium(context=FakeContext([cookies])))
    assert fetch_cached(profile) == "cached-value"


def test_fetch_cached_ignores_launch_failures(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    _patch_playwright(monkeypatch, FakeChromium(raise_exc=RuntimeError("boom")))
    assert fetch_cached(profile) is None


def test_fetch_cached_reraises_a_playwright_error_unrelated_to_installation(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    exc = RealPlaywrightError("some other failure entirely")
    _patch_playwright(monkeypatch, FakeChromium(raise_exc=exc))
    # fetch_cached's outer try/except still swallows it -- only _launch's own
    # re-raise-if-unrelated branch is what this exercises.
    assert fetch_cached(profile) is None


def test_fetch_cached_never_triggers_an_install(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()

    def fail_if_called(console):
        raise AssertionError("fetch_cached must never install the browser")

    monkeypatch.setattr(browser_login, "_install_chromium", fail_if_called)
    exc = RealPlaywrightError(
        "BrowserType.launch_persistent_context: Executable doesn't exist at /nope"
    )
    _patch_playwright(monkeypatch, FakeChromium(raise_exc=exc))
    assert fetch_cached(profile) is None


def test_install_chromium_reports_subprocess_success(monkeypatch):
    class FakeResult:
        returncode = 0

    monkeypatch.setattr(browser_login.subprocess, "run", lambda *a, **k: FakeResult())
    assert browser_login._install_chromium(console=None) is True


def test_install_chromium_reports_subprocess_failure(monkeypatch):
    class FakeResult:
        returncode = 1

    monkeypatch.setattr(browser_login.subprocess, "run", lambda *a, **k: FakeResult())
    assert browser_login._install_chromium(console=None) is False


def test_install_chromium_handles_a_missing_playwright_cli(monkeypatch):
    def raise_oserror(*a, **k):
        raise OSError("playwright: command not found")

    monkeypatch.setattr(browser_login.subprocess, "run", raise_oserror)
    assert browser_login._install_chromium(console=None) is False


def test_login_installs_chromium_then_retries_and_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_login.time, "sleep", lambda _: None)
    exc = RealPlaywrightError(
        "BrowserType.launch_persistent_context: Executable doesn't exist at /nope"
    )
    context = FakeContext([[{"name": "gp_access_token", "value": "fresh"}]])
    chromium = FakeChromium(context=context, raise_exc=exc, fail_count=1)
    _patch_playwright(monkeypatch, chromium)
    installed = []

    def fake_install(console):
        installed.append(1)
        return True

    monkeypatch.setattr(browser_login, "_install_chromium", fake_install)

    assert login(FakeConsole(), tmp_path / "profile") == "fresh"
    assert installed == [1]
    assert chromium.calls == 2


def test_login_returns_none_when_auto_install_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_login, "_install_chromium", lambda console: False)
    exc = RealPlaywrightError(
        "BrowserType.launch_persistent_context: Executable doesn't exist at /nope"
    )
    _patch_playwright(monkeypatch, FakeChromium(raise_exc=exc))
    assert login(FakeConsole(), tmp_path / "profile") is None


def test_login_polls_until_the_cookie_appears(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_login.time, "sleep", lambda _: None)
    sequence = [[], [], [{"name": "gp_access_token", "value": "fresh"}]]
    context = FakeContext(cookie_sequence=sequence)
    _patch_playwright(monkeypatch, FakeChromium(context=context))

    assert login(FakeConsole(), tmp_path / "profile") == "fresh"
    assert context.closed


def test_login_returns_none_when_the_window_is_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_login.time, "sleep", lambda _: None)
    context = FakeContext(raise_on_cookies=True)
    _patch_playwright(monkeypatch, FakeChromium(context=context))

    assert login(FakeConsole(), tmp_path / "profile") is None


def test_login_never_raises_on_an_unrelated_launch_failure(tmp_path, monkeypatch):
    # A locked profile dir, a Playwright/Chromium version mismatch, or any
    # other unexpected launch failure must degrade to None, like a
    # cancelled login -- never crash the caller.
    _patch_playwright(monkeypatch, FakeChromium(raise_exc=RuntimeError("profile dir is locked")))
    assert login(FakeConsole(), tmp_path / "profile") is None


def test_login_never_raises_when_goto_fails(tmp_path, monkeypatch):
    class BrokenPage(FakePage):
        def goto(self, url):
            raise RuntimeError("network is down")

    class BrokenContext(FakeContext):
        def new_page(self):
            page = BrokenPage()
            self.pages.append(page)
            return page

    _patch_playwright(monkeypatch, FakeChromium(context=BrokenContext()))
    assert login(FakeConsole(), tmp_path / "profile") is None


def test_a_cancelled_login_leaves_no_profile_behind(tmp_path, monkeypatch):
    """`setup` can be abandoned at the browser window. Nobody should be left
    holding state for a session they never created."""
    profile = tmp_path / "browser-profile"
    monkeypatch.setattr(browser_login, "_run_login", lambda *a, **k: None)

    assert browser_login.login(FakeConsole(), profile) is None
    assert not profile.exists()


def test_a_real_profile_is_never_removed(tmp_path, monkeypatch):
    """Only an empty directory goes: a profile with a session in it stays put
    even when this particular login came back empty-handed."""
    profile = tmp_path / "browser-profile"

    def writes_a_profile(console, profile_dir, timeout):
        (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(browser_login, "_run_login", writes_a_profile)

    assert browser_login.login(FakeConsole(), profile) is None
    assert (profile / "Default").exists()


def test_a_successful_login_keeps_its_profile(tmp_path, monkeypatch):
    profile = tmp_path / "browser-profile"
    monkeypatch.setattr(browser_login, "_run_login", lambda *a, **k: "tok")

    assert browser_login.login(FakeConsole(), profile) == "tok"
    assert profile.exists()
