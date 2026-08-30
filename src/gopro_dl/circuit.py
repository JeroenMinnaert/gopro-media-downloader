"""Global circuit breaker.

A GoPro-side outage during a multi-day run must not burn through the queue
marking thousands of files failed. When most recent operations fail for
systemic reasons, the breaker opens and workers park until a probe succeeds.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from .logging_setup import log_event


class CircuitBreaker:
    def __init__(
        self,
        window: int = 20,
        threshold: float = 0.8,
        min_samples: int = 5,
        base_cooldown: float = 60.0,
        max_cooldown: float = 900.0,
    ) -> None:
        self.window = window
        self.threshold = threshold
        self.min_samples = min_samples
        self.base_cooldown = base_cooldown
        self.max_cooldown = max_cooldown

        self._results: deque[bool] = deque(maxlen=window)
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._closed.set()
        self._cooldown = base_cooldown
        self._open_until = 0.0
        self._probe_in_flight = False

    @property
    def is_open(self) -> bool:
        return not self._closed.is_set()

    def record(self, ok: bool, systemic: bool = False) -> None:
        """Record an operation. Only systemic failures can trip the breaker."""
        with self._lock:
            if ok:
                self._results.append(True)
                if self.is_open or self._probe_in_flight:
                    self._reset_locked()
                return
            if not systemic:
                return  # a per-file 404 says nothing about GoPro's health
            self._results.append(False)
            if self.is_open or len(self._results) < self.min_samples:
                return
            failures = sum(1 for r in self._results if not r)
            if failures / len(self._results) >= self.threshold:
                self._open_locked()

    def _open_locked(self) -> None:
        self._closed.clear()
        self._open_until = time.monotonic() + self._cooldown
        self._probe_in_flight = False
        log_event(
            logging.ERROR,
            "circuit_open",
            cooldown_s=round(self._cooldown, 1),
            window_failures=sum(1 for r in self._results if not r),
            window_size=len(self._results),
        )

    def _reset_locked(self) -> None:
        self._results.clear()
        self._cooldown = self.base_cooldown
        self._probe_in_flight = False
        if not self._closed.is_set():
            log_event(logging.INFO, "circuit_closed")
        self._closed.set()

    def wait(self, shutdown: threading.Event | None = None) -> bool:
        """Block while the breaker is open.

        Returns True when the caller may proceed. Exactly one waiter is
        released as a probe when the cooldown elapses; the rest keep waiting
        until that probe reports back.
        """
        while True:
            if shutdown is not None and shutdown.is_set():
                return False
            if self._closed.wait(timeout=1.0):
                return True
            with self._lock:
                if self.is_open and not self._probe_in_flight and time.monotonic() >= self._open_until:
                    self._probe_in_flight = True
                    log_event(logging.WARNING, "circuit_half_open_probe")
                    return True

    def probe_failed(self) -> None:
        """The half-open probe failed: back off harder."""
        with self._lock:
            if not self._probe_in_flight:
                return
            self._probe_in_flight = False
            self._cooldown = min(self._cooldown * 2, self.max_cooldown)
            self._open_until = time.monotonic() + self._cooldown
            log_event(logging.ERROR, "circuit_probe_failed", cooldown_s=round(self._cooldown, 1))
