"""Token buckets, circuit breakers and the fetch gate (spec sections 6 Stage 4, 16, 17).

Scope note: this state is per worker process. The spec calls the bucket and the
breaker "global"; with more than one worker process each gets its own, so the
effective rate is N x JINA_RPM. Backing these with Redis is a step-8 concern and
the interfaces here are deliberately narrow so that swap is mechanical.
"""

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field


class TokenBucket:
    """Rate limiter measured in requests per minute.

    Capacity defaults to a tenth of the minute budget so a burst cannot spend the
    whole window at once (at JINA_RPM=500 that is a 50-request burst).
    """

    def __init__(self, rate_per_minute: int, capacity: int | None = None) -> None:
        self.rate_per_minute = rate_per_minute
        self._rate = rate_per_minute / 60.0
        # Burst capacity is deliberately small: a tenth of the minute budget could fire 50
        # requests instantly and trip "Per UID rate limit exceeded" even while the
        # average stayed under the cap.
        self._capacity = float(capacity if capacity is not None else max(1, rate_per_minute // 50))
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        self._penalty_factor = 1.0
        self._penalty_until = 0.0

    def _effective_rate(self) -> float:
        if time.monotonic() < self._penalty_until:
            return self._rate * self._penalty_factor
        return self._rate

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        self._updated = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._effective_rate())

    def penalize(self, fraction: float = 0.25, seconds: float = 60.0) -> None:
        """Slow the bucket after a provider 429 (spec 17, level 1)."""
        self._penalty_factor = max(0.05, 1.0 - fraction)
        self._penalty_until = time.monotonic() + seconds

    async def acquire(self, n: int = 1) -> None:
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    return
                wait = (n - self._tokens) / max(self._effective_rate(), 1e-9)
            await asyncio.sleep(min(wait, 5.0))

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens


class CircuitOpen(Exception):
    """Raised when a provider's breaker is open and the call must not be made."""

    def __init__(self, provider: str, retry_in: float) -> None:
        super().__init__(f"{provider} circuit open, retry in {retry_in:.0f}s")
        self.provider = provider
        self.retry_in = retry_in


@dataclass
class CircuitBreaker:
    """Per-provider breaker (spec 17, level 3).

    Opens when more than `threshold` of the last `window` calls failed, stays open
    for `open_seconds`, then admits `probes` calls and closes if `probes_needed`
    of them succeed. `min_samples` stops a handful of early failures opening it.

    `probes_needed` is deliberately BELOW `probes`. At 4 of 5 the bar to recover was
    80% success, while the thing that opened the breaker was a failure rate over
    `threshold` -- so half-open usually failed and reopened immediately. Over the
    16k run that oscillation ran for seven hours: stretches with the breaker quiet
    found 56-62% of addresses, stretches with it thrashing found 24-33%.

    Repeated opens escalate the window (`open_seconds` doubling up to
    `max_open_seconds`) so a real outage is waited out once instead of being probed
    every minute. The escalation resets only after the breaker has held closed for
    `recovery_multiple` x `open_seconds` -- closing and reopening at once is the
    same outage continuing, not a recovery.
    """

    provider: str
    window: int = 200
    threshold: float = 0.30
    open_seconds: float = 60.0
    max_open_seconds: float = 300.0
    probes: int = 5
    probes_needed: int = 3
    min_samples: int = 20
    recovery_multiple: float = 4.0

    _outcomes: deque[bool] = field(default_factory=deque, init=False)
    _state: str = field(default="closed", init=False)
    _opened_at: float = field(default=0.0, init=False)
    _closed_at: float = field(default=0.0, init=False)
    # The CURRENT open window, which escalation grows past `open_seconds`.
    _open_for: float = field(default=0.0, init=False)
    _consecutive_opens: int = field(default=0, init=False)
    _probe_results: list[bool] = field(default_factory=list, init=False)
    opened_count: int = field(default=0, init=False)
    # What actually pushed it open, so a trip is diagnosable after the fact.
    failure_reasons: dict[str, int] = field(default_factory=dict, init=False)
    last_open_reasons: dict[str, int] = field(default_factory=dict, init=False)

    @property
    def state(self) -> str:
        if self._state == "open" and time.monotonic() - self._opened_at >= self._open_for:
            self._state = "half_open"
            self._probe_results = []
        return self._state

    @property
    def open_for(self) -> float:
        """The current open window, after escalation. For logging."""
        return self._open_for

    def seconds_until_close(self) -> float:
        """How much of the current open window is left; 0 when it is not open.

        Callers that can afford to wait (a whole domain, which has fetched nothing
        yet) use this to sleep until the provider is plausibly back, rather than
        guessing at a fixed backoff.
        """
        if self.state != "open":
            return 0.0
        return max(0.0, self._open_for - (time.monotonic() - self._opened_at))

    def check(self) -> None:
        """Raise CircuitOpen if this call must not be made."""
        state = self.state
        if state == "open":
            raise CircuitOpen(self.provider, self._open_for - (time.monotonic() - self._opened_at))
        if state == "half_open" and len(self._probe_results) >= self.probes:
            # Probes are in flight; hold callers back until they resolve.
            raise CircuitOpen(self.provider, 1.0)

    def record(self, ok: bool, reason: str = "") -> None:
        if not ok and reason:
            self.failure_reasons[reason] = self.failure_reasons.get(reason, 0) + 1
        state = self.state
        if state == "half_open":
            self._probe_results.append(ok)
            if len(self._probe_results) >= self.probes:
                if sum(self._probe_results) >= self.probes_needed:
                    self._close()
                else:
                    self._open()
            return

        self._outcomes.append(ok)
        while len(self._outcomes) > self.window:
            self._outcomes.popleft()
        if len(self._outcomes) < self.min_samples:
            return
        failures = sum(1 for o in self._outcomes if not o)
        if failures / len(self._outcomes) > self.threshold:
            self._open()

    def _open(self) -> None:
        now = time.monotonic()
        if self._closed_at and now - self._closed_at > self.open_seconds * self.recovery_multiple:
            self._consecutive_opens = 0          # it genuinely recovered in between
        self._open_for = min(
            self.open_seconds * (2 ** self._consecutive_opens), self.max_open_seconds
        )
        self._consecutive_opens += 1
        self.last_open_reasons = dict(self.failure_reasons)
        self.failure_reasons = {}
        self._state = "open"
        self._opened_at = now
        self._outcomes.clear()
        self._probe_results = []
        self.opened_count += 1

    def _close(self) -> None:
        self._state = "closed"
        self._closed_at = time.monotonic()
        self._outcomes.clear()
        self._probe_results = []


def jittered(base: float, fraction: float = 0.5) -> float:
    """base plus 0-`fraction` random jitter (spec 17, level 1)."""
    return base * (1.0 + random.random() * fraction)


class FetchGate:
    """Everything a fetch must pass through: breaker, bucket, global and per-domain slots."""

    def __init__(
        self,
        *,
        rate_per_minute: int | None,
        global_concurrency: int,
        per_domain_concurrency: int,
        provider: str = "jina",
    ) -> None:
        self.bucket = TokenBucket(rate_per_minute) if rate_per_minute else None
        self.breaker = CircuitBreaker(provider)
        self._global = asyncio.Semaphore(global_concurrency)
        self._per_domain_limit = per_domain_concurrency
        self._per_domain: dict[str, asyncio.Semaphore] = {}

    def _domain_sem(self, domain: str) -> asyncio.Semaphore:
        sem = self._per_domain.get(domain)
        if sem is None:
            sem = asyncio.Semaphore(self._per_domain_limit)
            self._per_domain[domain] = sem
        return sem

    def release_domain(self, domain: str) -> None:
        """Drop a domain's semaphore once it is finished, so the dict does not grow to 50k."""
        sem = self._per_domain.get(domain)
        if sem is not None and sem._value == self._per_domain_limit:  # noqa: SLF001
            del self._per_domain[domain]

    class _Slot:
        def __init__(self, gate: "FetchGate", domain: str) -> None:
            self._gate = gate
            self._domain = domain

        # An open breaker means "pause", not "fail". Raising here failed every waiting
        # domain instantly -- one 429 burst wrote off thousands of rows in seconds.
        # The cap stays modest because giving up here is no longer the end of the
        # domain: process_domain treats CircuitOpen as retryable and comes back once
        # the breaker's window has run, so a slot never blocks for a whole outage.
        MAX_BREAKER_WAIT_S = 30.0

        async def __aenter__(self) -> "FetchGate._Slot":
            g = self._gate
            waited = 0.0
            while True:
                try:
                    g.breaker.check()
                    break
                except CircuitOpen as e:
                    # Jittered so every waiter does not resume in lockstep and trip it again.
                    nap = min(max(e.retry_in, 0.5), 5.0) * (1.0 + random.random() * 0.5)
                    if waited + nap > self.MAX_BREAKER_WAIT_S:
                        raise
                    await asyncio.sleep(nap)
                    waited += nap
            await g._global.acquire()  # noqa: SLF001
            try:
                await g._domain_sem(self._domain).acquire()  # noqa: SLF001
            except BaseException:
                g._global.release()  # noqa: SLF001
                raise
            try:
                if g.bucket is not None:
                    await g.bucket.acquire()
            except BaseException:
                g._domain_sem(self._domain).release()  # noqa: SLF001
                g._global.release()  # noqa: SLF001
                raise
            return self

        async def __aexit__(self, *exc: object) -> None:
            g = self._gate
            g._domain_sem(self._domain).release()  # noqa: SLF001
            g._global.release()  # noqa: SLF001

    def slot(self, domain: str) -> "FetchGate._Slot":
        return FetchGate._Slot(self, domain)
