"""Token handling and the gate that pauses a run when the token expires.

Design note: a 1 TB run outlives a GoPro JWT, so expiry is a normal event, not
an error. On a 401/403 from api.gopro.com every worker parks here, the user
refreshes the token, and the run resumes from the exact byte offset. Nothing
is re-downloaded.

Crucially, a 403 from the *CDN* is NOT token expiry -- signed media URLs are
time-limited and expire routinely mid-download. Those are handled in the
downloader by re-requesting a fresh URL, and never reach this gate.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .logging_setup import log_event


class TokenError(RuntimeError):
    pass


class TokenProvider:
    """Supplies the bearer token, and can re-read it while the run is live."""

    def __init__(self, token: str | None = None, token_file: Path | None = None) -> None:
        self._token_file = token_file
        self._explicit = token
        self._lock = threading.Lock()
        self._token = token or self._read_file()
        if not self._token:
            where = f" or {token_file}" if token_file else ""
            raise TokenError(
                f"No GoPro token found. Set GOPRO_TOKEN, --token, or a token file{where}. "
                "See the README for how to copy it out of Chrome."
            )

    def _read_file(self) -> str | None:
        if not self._token_file:
            return None
        try:
            text = self._token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        # Tolerate a pasted "Authorization: Bearer eyJ..." line.
        for prefix in ("Authorization:", "authorization:"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        if text.lower().startswith("bearer "):
            text = text[7:].strip()
        return text or None

    @property
    def token(self) -> str:
        with self._lock:
            return self._token

    @property
    def source_description(self) -> str:
        if self._token_file:
            return str(self._token_file)
        return "--token/GOPRO_TOKEN"

    def reload(self) -> bool:
        """Re-read the token file. True if the token actually changed."""
        new = self._read_file()
        if not new:
            return False
        with self._lock:
            if new == self._token:
                return False
            self._token = new
        return True

    def set(self, token: str) -> None:
        with self._lock:
            self._token = token.strip()


class AuthGate:
    """Barrier that holds every worker while the token is being refreshed."""

    def __init__(self) -> None:
        self._ok = threading.Event()
        self._ok.set()
        self._lock = threading.Lock()
        self._generation = 0

    @property
    def is_paused(self) -> bool:
        return not self._ok.is_set()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def wait(self, shutdown: threading.Event | None = None) -> bool:
        """Block while paused. False means the run is shutting down."""
        while not self._ok.wait(timeout=0.5):
            if shutdown is not None and shutdown.is_set():
                return False
        return not (shutdown is not None and shutdown.is_set())

    def trip(self, reason: str) -> bool:
        """Pause the run. True if this caller is the one that tripped it."""
        with self._lock:
            if not self._ok.is_set():
                return False
            self._ok.clear()
            self._generation += 1
            log_event(logging.WARNING, "auth_paused", reason=reason)
            return True

    def resume(self) -> None:
        with self._lock:
            if not self._ok.is_set():
                log_event(logging.INFO, "auth_resumed")
            self._ok.set()


def refresh_token_interactively(
    provider: TokenProvider,
    validate,
    console,
    non_interactive: bool = False,
    poll_seconds: float = 30.0,
    timeout_seconds: float = 3600.0,
    shutdown: threading.Event | None = None,
) -> bool:
    """Get a working token back in place. `validate(token) -> bool`.

    Interactive: prompt, re-read the token file, validate, repeat.
    Non-interactive: poll the token file until it changes to a valid token.
    """
    if non_interactive:
        deadline = time.monotonic() + timeout_seconds
        console.print(
            f"[yellow]Token rejected. Waiting for an updated token in "
            f"{provider.source_description}...[/yellow]"
        )
        while time.monotonic() < deadline:
            if shutdown is not None and shutdown.is_set():
                return False
            time.sleep(poll_seconds)
            if provider.reload() and validate(provider.token):
                return True
        log_event(logging.ERROR, "auth_refresh_timeout")
        return False

    while True:
        if shutdown is not None and shutdown.is_set():
            return False
        console.print()
        console.print("[bold yellow]GoPro token expired or rejected.[/bold yellow]")
        console.print(
            f"  1. Open [link]https://gopro.com/media-library/[/link] in Chrome (log in if needed)\n"
            f"  2. DevTools (Cmd+Option+I) -> Application -> Cookies -> gopro.com -> [bold]gp_access_token[/bold]\n"
            f"  3. Copy the value into [bold]{provider.source_description}[/bold]"
        )
        try:
            answer = console.input(
                "Press [bold]Enter[/bold] once updated, paste a token directly, or Ctrl-C to stop: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return False

        if answer:
            provider.set(answer)
        else:
            provider.reload()

        if validate(provider.token):
            console.print("[green]Token accepted. Resuming.[/green]")
            return True
        console.print("[red]That token was rejected too.[/red]")
