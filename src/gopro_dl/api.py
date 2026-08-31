"""HTTP client for api.gopro.com: pagination, retry/backoff, auth transport."""

from __future__ import annotations

import email.utils
import logging
import random
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from .auth import AuthGate, TokenProvider
from .circuit import CircuitBreaker
from .logging_setup import log_event
from .models import SEARCH_FIELDS, MediaItem

API_HOST = "https://api.gopro.com"
ACCEPT_SEARCH = "application/vnd.gopro.jk.media.search+json; version=2.0.0"
ACCEPT_MEDIA = "application/vnd.gopro.jk.media+json; version=2.0.0"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
AUTH_STATUS = frozenset({401, 403})


class AuthExpired(RuntimeError):
    """api.gopro.com rejected our bearer token."""


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, systemic: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.systemic = systemic


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        # Anything unparseable just means "no hint" -- a malformed header from
        # a proxy must not take down the retry path it appears on.
        return None
    if parsed is None:
        return None
    import datetime as _dt

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    delay = (parsed - _dt.datetime.now(_dt.UTC)).total_seconds()
    return max(delay, 0.0)


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 120.0) -> float:
    """Exponential backoff with full jitter."""
    return random.uniform(0, min(base * (2**attempt), cap))


class GoProClient:
    def __init__(
        self,
        tokens: TokenProvider,
        gate: AuthGate,
        breaker: CircuitBreaker | None = None,
        user_id: str | None = None,
        concurrency: int = 3,
        max_attempts: int = 6,
        shutdown: threading.Event | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.tokens = tokens
        self.gate = gate
        self.breaker = breaker or CircuitBreaker()
        self.user_id = user_id
        self.max_attempts = max_attempts
        self.shutdown = shutdown or threading.Event()
        self._sleep = sleep
        self._auth_mode = "bearer"  # falls back to "cookie" if the header is refused
        self._mode_lock = threading.Lock()

        self.client = httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=30.0),
            limits=httpx.Limits(
                max_connections=concurrency + 2, max_keepalive_connections=concurrency
            ),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> GoProClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- auth transport ----------------------------------------------------

    def _auth_kwargs(self, mode: str | None = None) -> dict[str, Any]:
        mode = mode or self._auth_mode
        token = self.tokens.token
        if mode == "cookie":
            # Sent as a header rather than httpx's per-request cookies=, which
            # is deprecated and has ambiguous persistence semantics.
            cookie = f"gp_access_token={token}"
            if self.user_id:
                cookie += f"; gp_user_id={self.user_id}"
            return {"headers": {"Cookie": cookie}}
        return {"headers": {"Authorization": f"Bearer {token}"}}

    def _remember_mode(self, mode: str) -> None:
        with self._mode_lock:
            if self._auth_mode != mode:
                self._auth_mode = mode
                log_event(logging.INFO, "auth_mode_selected", mode=mode)

    @property
    def auth_mode(self) -> str:
        return self._auth_mode

    # -- request plumbing --------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        accept: str = ACCEPT_MEDIA,
        bypass_gate: bool = False,
        **kwargs,
    ) -> httpx.Response:
        """One API call, with backoff, breaker integration and auth fallback.

        Raises AuthExpired when the bearer token itself is dead -- the caller
        pauses the run rather than failing files.

        `bypass_gate` is for the token-validation call made *while* the gate is
        held: it is the request that decides whether the pause can be lifted,
        so waiting on the gate would deadlock the run.
        """
        last_error: Exception | None = None
        # Popped once, outside the loop: popping per attempt would strip the
        # caller's headers and every retry after the first would go without.
        caller_headers = kwargs.pop("headers", {})

        for attempt in range(self.max_attempts):
            if self.shutdown.is_set():
                raise ApiError("shutting down")
            if not bypass_gate and not self.gate.wait(self.shutdown):
                raise ApiError("shutting down")
            if not self.breaker.wait(self.shutdown):
                raise ApiError("shutting down")

            auth = self._auth_kwargs()
            headers = {"Accept": accept, **auth.pop("headers", {}), **caller_headers}

            try:
                response = self.client.request(method, url, headers=headers, **auth, **kwargs)
            except httpx.HTTPError as exc:
                last_error = exc
                self.breaker.record(False, systemic=True)
                self.breaker.probe_failed()
                log_event(
                    logging.WARNING, "request_transport_error", url=url, attempt=attempt, error=str(exc)
                )
                self._sleep(backoff_delay(attempt))
                continue

            status = response.status_code

            if status in AUTH_STATUS:
                # Try the cookie transport once before concluding the token is dead.
                retry = None
                if self._auth_mode == "bearer":
                    alt = self._auth_kwargs("cookie")
                    alt_headers = {"Accept": accept, **alt.pop("headers", {}), **caller_headers}
                    try:
                        retry = self.client.request(method, url, headers=alt_headers, **alt, **kwargs)
                    except httpx.HTTPError:
                        retry = None
                if retry is not None and retry.status_code < 400:
                    self._remember_mode("cookie")
                    self.breaker.record(True)
                    return retry
                if retry is None or retry.status_code in AUTH_STATUS:
                    self.breaker.record(True)  # the service answered; it is not an outage
                    raise AuthExpired(f"{status} from {url}")
                # The cookie transport got past the auth wall onto some other
                # error: the token is not the problem, but an error body is not
                # a result either. Judge it like any other response rather than
                # handing a 500 back as though it were the download JSON.
                response, status = retry, retry.status_code

            if status in RETRY_STATUS:
                last_error = ApiError(f"HTTP {status} from {url}", status=status, systemic=True)
                self.breaker.record(False, systemic=True)
                self.breaker.probe_failed()
                delay = parse_retry_after(response.headers.get("Retry-After"))
                computed = backoff_delay(attempt)
                log_event(
                    logging.WARNING,
                    "request_retry",
                    url=url,
                    status=status,
                    attempt=attempt,
                    retry_after=delay,
                )
                self._sleep(max(delay or 0.0, computed))
                continue

            self.breaker.record(True)
            if status >= 400:
                raise ApiError(f"HTTP {status} from {url}: {response.text[:200]}", status=status)
            return response

        raise ApiError(f"gave up after {self.max_attempts} attempts: {last_error}", systemic=True)

    # -- endpoints ---------------------------------------------------------

    def validate_token(self, token: str | None = None) -> dict[str, Any] | None:
        """Check a token against /media/user. Returns the account JSON or None."""
        if token is not None:
            self.tokens.set(token)
        try:
            # bypass_gate: this call is how a paused run gets unpaused
            response = self.request("GET", f"{API_HOST}/media/user", bypass_gate=True)
        except AuthExpired:
            return None
        except ApiError as exc:
            if exc.systemic:
                # api.gopro.com was never actually reached. That says nothing
                # about the token, and reporting it as a rejection would send
                # the user off fetching a fresh one for nothing.
                raise
            return None
        try:
            data = response.json()
        except ValueError:
            return {}
        if isinstance(data, dict) and data.get("id") and not self.user_id:
            self.user_id = str(data["id"])
        return data if isinstance(data, dict) else {}

    def iter_media(
        self,
        types: tuple[str, ...],
        per_page: int = 100,
        start_page: int = 1,
        on_page: Callable[[int, int], None] | None = None,
    ) -> Iterator[MediaItem]:
        """Page through the whole library."""
        page = start_page
        total_pages = 1
        while page <= total_pages:
            params = {
                "processing_states": "rendering,pretranscoding,transcoding,ready",
                "fields": SEARCH_FIELDS,
                "type": ",".join(types),
                "page": page,
                "per_page": per_page,
            }
            response = self.request(
                "GET", f"{API_HOST}/media/search", accept=ACCEPT_SEARCH, params=params
            )
            data = response.json()
            pages = data.get("_pages") or {}
            total_pages = int(pages.get("total_pages") or 1)
            if on_page:
                on_page(page, total_pages)
            for record in (data.get("_embedded") or {}).get("media") or []:
                yield MediaItem.from_json(record)
            page += 1

    def get_download(self, media_id: str) -> dict[str, Any]:
        """Fetch the (time-limited) source URLs for one media item."""
        response = self.request("GET", f"{API_HOST}/media/{media_id}/download", accept=ACCEPT_SEARCH)
        return response.json()
