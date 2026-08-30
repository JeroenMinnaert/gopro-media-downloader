"""The breaker: an outage must not burn the queue into thousands of failures."""

import threading

from gopro_dl.circuit import CircuitBreaker


def test_per_file_errors_never_trip_it():
    breaker = CircuitBreaker(window=10, min_samples=3)
    for _ in range(20):
        breaker.record(False, systemic=False)  # e.g. a 404 on one media item
    assert not breaker.is_open


def test_systemic_failures_open_it():
    breaker = CircuitBreaker(window=10, threshold=0.8, min_samples=5)
    for _ in range(5):
        breaker.record(False, systemic=True)
    assert breaker.is_open


def test_success_closes_it_and_resets_the_cooldown():
    breaker = CircuitBreaker(window=10, min_samples=3, base_cooldown=0.01)
    for _ in range(5):
        breaker.record(False, systemic=True)
    assert breaker.is_open

    assert breaker.wait(threading.Event()) is True  # released as the probe
    breaker.record(True)
    assert not breaker.is_open


def test_a_failed_probe_backs_off_further():
    breaker = CircuitBreaker(window=10, min_samples=3, base_cooldown=0.01, max_cooldown=1.0)
    for _ in range(5):
        breaker.record(False, systemic=True)
    breaker.wait(threading.Event())
    before = breaker._cooldown
    breaker.probe_failed()
    assert breaker._cooldown > before
    assert breaker.is_open


def test_wait_returns_false_on_shutdown():
    breaker = CircuitBreaker(window=10, min_samples=3, base_cooldown=60)
    for _ in range(5):
        breaker.record(False, systemic=True)
    shutdown = threading.Event()
    shutdown.set()
    assert breaker.wait(shutdown) is False
