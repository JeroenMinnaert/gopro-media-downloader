"""Get the gp_access_token cookie by driving a real, visible browser login.

No credential handling here: the login happens on GoPro's own page inside a
Chromium window Playwright opens, and this module only ever reads the
resulting session cookie back out of that browser's own cookie jar -- never
a password, and never another browser's existing cookie store. Login state
persists in the profile directory the caller passes in (`fetch_cached`), so
a later call can often find the cookie again without popping a window at
all.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import sys
import time
from pathlib import Path

from .logging_setup import log_event

LOGIN_URL = "https://gopro.com/media-library/"
COOKIE_NAME = "gp_access_token"

INSTALL_HINT = (
    "Couldn't install Playwright's browser automatically. "
    "Run `playwright install chromium` yourself, then try again."
)


class BrowserNotInstalled(RuntimeError):
    pass


def _ensure_profile_dir(profile_dir: Path) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.chmod(0o700)


def _extract(cookies) -> str | None:
    for cookie in cookies:
        if cookie["name"] == COOKIE_NAME and cookie["value"]:
            return cookie["value"]
    return None


def _install_chromium(console) -> bool:
    """Run `playwright install chromium` as a subprocess. True on success."""
    if console is not None:
        console.print(
            "[yellow]Downloading the browser used for GoPro login "
            "(one-time, ~250MB)...[/yellow]"
        )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log_event(logging.WARNING, "playwright_install_failed", error=str(exc))
        return False
    if result.returncode != 0:
        log_event(logging.WARNING, "playwright_install_failed", returncode=result.returncode)
        return False
    return True


def _launch(p, profile_dir: Path, *, headless: bool, auto_install: bool, console=None):
    """Launch the persistent context, installing Chromium once if allowed."""
    from playwright.sync_api import Error as PlaywrightError

    try:
        return p.chromium.launch_persistent_context(str(profile_dir), headless=headless)
    except PlaywrightError as exc:
        if "Executable doesn't exist" not in str(exc):
            raise
        if not auto_install or not _install_chromium(console):
            raise BrowserNotInstalled(INSTALL_HINT) from exc
        return p.chromium.launch_persistent_context(str(profile_dir), headless=headless)


def fetch_cached(profile_dir: Path) -> str | None:
    """Silently check the persisted profile for a still-present session cookie.

    Never opens a visible window, never installs the browser, and never
    raises: any failure (no profile yet, browser not installed, a locked
    profile) just means "nothing cached".
    """
    if not profile_dir.exists():
        return None
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            context = _launch(p, profile_dir, headless=True, auto_install=False)
            try:
                return _extract(context.cookies())
            finally:
                context.close()
    except Exception as exc:
        log_event(logging.DEBUG, "browser_cached_check_failed", error=str(exc))
        return None


def login(console, profile_dir: Path, timeout_seconds: float = 300.0) -> str | None:
    """Open a visible browser window at the GoPro login page and wait for it.

    Polls the persisted profile's cookies until `gp_access_token` shows up,
    the window is closed, or `timeout_seconds` elapses (default 5 minutes).
    Installs Chromium itself (via `playwright install chromium`) the first
    time it's needed. Like `fetch_cached`, this never raises: if that install
    fails, it prints why and returns None -- callers treat it the same as a
    cancelled or timed-out login.
    """
    _ensure_profile_dir(profile_dir)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            context = _launch(p, profile_dir, headless=False, auto_install=True, console=console)
        except BrowserNotInstalled as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            return None

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL)
        console.print(
            "[bold]A browser window has opened.[/bold] Log into GoPro there -- "
            "this continues automatically once you're signed in, or Ctrl-C to give up."
        )

        deadline = time.monotonic() + timeout_seconds
        token = None
        try:
            while time.monotonic() < deadline:
                try:
                    token = _extract(context.cookies())
                except Exception:
                    break  # the window was closed
                if token:
                    break
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            with contextlib.suppress(Exception):
                context.close()
        if token is None:
            log_event(logging.INFO, "browser_login_no_cookie")
        return token
