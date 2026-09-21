"""Token bucket, circuit breaker and fetch gate (spec 16, 17 level 3)."""

import asyncio
import time

import pytest

from app.ratelimit import CircuitBreaker, CircuitOpen, FetchGate, TokenBucket, jittered


async def test_bucket_allows_burst_up_to_capacity():
    b = TokenBucket(rate_per_minute=600, capacity=5)
    t0 = time.monotonic()
    for _ in range(5):
        await b.acquire()
    assert time.monotonic() - t0 < 0.05


async def test_bucket_throttles_past_capacity():
    # 600/min = 10/s, so the 6th token after a 5-burst costs ~0.1s.
    b = TokenBucket(rate_per_minute=600, capacity=5)
    for _ in range(5):
        await b.acquire()
    t0 = time.monotonic()
    await b.acquire()
    assert time.monotonic() - t0 >= 0.05


async def test_bucket_penalty_slows_refill():
    b = TokenBucket(rate_per_minute=600, capacity=1)
    await b.acquire()
    b.penalize(0.75, seconds=60)          # quarter speed
    t0 = time.monotonic()
    await b.acquire()
    assert time.monotonic() - t0 >= 0.2   # 0.1s at full rate, ~0.4s at quarter


def test_breaker_stays_closed_below_min_samples():
    cb = CircuitBreaker("jina", min_samples=20)
    for _ in range(10):
        cb.record(False)
    assert cb.state == "closed"
    cb.check()


def test_breaker_opens_above_threshold():
    cb = CircuitBreaker("jina", window=200, threshold=0.30, min_samples=20)
    for i in range(100):
        cb.record(i % 3 != 0)   # ~33% failures, over the 30% threshold
    assert cb.state == "open"
    with pytest.raises(CircuitOpen):
        cb.check()


def test_breaker_half_opens_then_closes_on_good_probes():
    cb = CircuitBreaker("jina", open_seconds=0.2, min_samples=20)
    for _ in range(25):
        cb.record(False)
    assert cb.state == "open"
    with pytest.raises(CircuitOpen):
        cb.check()

    time.sleep(0.25)
    assert cb.state == "half_open"
    for _ in range(4):
        cb.record(True)
    cb.record(False)                      # 4 of 5 succeeded -> close
    assert cb.state == "closed"
    assert cb.opened_count == 1
    cb.check()


def test_breaker_reopens_when_probes_fail():
    cb = CircuitBreaker("jina", open_seconds=0.2, min_samples=20)
    for _ in range(25):
        cb.record(False)
    assert cb.opened_count == 1

    time.sleep(0.25)
    assert cb.state == "half_open"
    for _ in range(5):
        cb.record(False)                  # every probe fails -> open again
    assert cb.opened_count == 2
    with pytest.raises(CircuitOpen):
        cb.check()


def test_jitter_is_within_declared_band():
    for _ in range(200):
        v = jittered(2.0, 0.5)
        assert 2.0 <= v <= 3.0


async def test_gate_enforces_per_domain_concurrency():
    gate = FetchGate(
        rate_per_minute=None, global_concurrency=50, per_domain_concurrency=2, provider="test"
    )
    live = 0
    peak = 0

    async def one():
        nonlocal live, peak
        async with gate.slot("acme.com"):
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.02)
            live -= 1

    await asyncio.gather(*(one() for _ in range(10)))
    assert peak <= 2


async def test_gate_enforces_global_concurrency_across_domains():
    gate = FetchGate(
        rate_per_minute=None, global_concurrency=3, per_domain_concurrency=2, provider="test"
    )
    live = 0
    peak = 0

    async def one(i: int):
        nonlocal live, peak
        async with gate.slot(f"d{i}.com"):
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.02)
            live -= 1

    await asyncio.gather(*(one(i) for i in range(12)))
    assert peak <= 3


async def test_gate_refuses_when_breaker_open():
    gate = FetchGate(
        rate_per_minute=None, global_concurrency=5, per_domain_concurrency=2, provider="jina"
    )
    for _ in range(30):
        gate.breaker.record(False)
    slot = gate.slot("acme.com")
    slot.MAX_BREAKER_WAIT_S = 0.05          # do not wait out the real 60s window
    with pytest.raises(CircuitOpen):
        async with slot:
            pass


async def test_gate_releases_slots_on_error():
    gate = FetchGate(
        rate_per_minute=None, global_concurrency=2, per_domain_concurrency=1, provider="test"
    )
    for _ in range(5):
        with pytest.raises(RuntimeError):
            async with gate.slot("acme.com"):
                raise RuntimeError("boom")
    # If slots leaked, this would deadlock rather than complete.
    async with gate.slot("acme.com"):
        pass


async def test_open_breaker_waits_instead_of_failing_every_caller():
    """A 429 burst used to write off every waiting domain instantly."""
    gate = FetchGate(
        rate_per_minute=None, global_concurrency=5, per_domain_concurrency=2, provider="jina"
    )
    gate.breaker.open_seconds = 0.4
    for _ in range(30):
        gate.breaker.record(False)
    assert gate.breaker.state == "open"

    t0 = time.monotonic()
    async with gate.slot("acme.com"):      # waits out the window rather than raising
        pass
    assert 0.3 < time.monotonic() - t0 < 12.0


async def test_breaker_still_gives_up_after_the_max_wait():
    gate = FetchGate(
        rate_per_minute=None, global_concurrency=5, per_domain_concurrency=2, provider="jina"
    )
    gate.breaker.open_seconds = 600.0
    for _ in range(30):
        gate.breaker.record(False)
    slot = gate.slot("acme.com")
    slot.MAX_BREAKER_WAIT_S = 0.2
    with pytest.raises(CircuitOpen):
        async with slot:
            pass


# --- recovery bar and escalation (the seven-hour oscillation) -------------


def test_half_open_bar_sits_below_the_rate_that_opened_it():
    """At 4 of 5 the bar to recover was 80% success while the breaker opened at a 30%
    failure rate, so half-open usually failed and reopened at once."""
    cb = CircuitBreaker("jina", open_seconds=0.05, min_samples=20)
    assert cb.probes_needed < cb.probes
    for _ in range(25):
        cb.record(False)
    time.sleep(0.06)
    assert cb.state == "half_open"
    for ok in (True, True, True, False, False):    # 3 of 5, the old bar would reopen
        cb.record(ok)
    assert cb.state == "closed"
    assert cb.opened_count == 1


def test_repeated_opens_escalate_the_window():
    cb = CircuitBreaker("jina", open_seconds=0.05, max_open_seconds=0.4, min_samples=20)
    for _ in range(25):
        cb.record(False)
    assert cb.open_for == pytest.approx(0.05)

    for expected in (0.1, 0.2, 0.4, 0.4):          # doubles, then holds at the ceiling
        time.sleep(cb.open_for + 0.01)
        assert cb.state == "half_open"
        for _ in range(cb.probes):
            cb.record(False)
        assert cb.open_for == pytest.approx(expected)


def test_escalation_resets_once_it_has_held_closed():
    """Closing and reopening immediately is the same outage; a real recovery is not."""
    cb = CircuitBreaker(
        "jina", open_seconds=0.05, min_samples=20, recovery_multiple=1.0
    )
    for _ in range(25):
        cb.record(False)
    time.sleep(0.06)
    assert cb.state == "half_open"
    for _ in range(cb.probes):
        cb.record(True)
    assert cb.state == "closed"

    time.sleep(0.08)                               # > open_seconds * recovery_multiple
    for _ in range(25):
        cb.record(False)
    assert cb.open_for == pytest.approx(0.05)      # back to the base window, not doubled


def test_seconds_until_close_reports_the_remaining_window():
    cb = CircuitBreaker("jina", open_seconds=10.0, min_samples=20)
    assert cb.seconds_until_close() == 0.0
    for _ in range(25):
        cb.record(False)
    assert 9.0 < cb.seconds_until_close() <= 10.0
