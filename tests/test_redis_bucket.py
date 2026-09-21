"""The shared Jina budget (RedisTokenBucket) against fakeredis's Lua engine."""

import asyncio
import time

import fakeredis
import pytest

from app.ratelimit import RedisTokenBucket


def client():
    return fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer())


async def test_two_processes_share_one_budget():
    """Two buckets on one Redis are one limit, not two."""
    shared = client()
    a = RedisTokenBucket("redis://unused", 600, client=shared, capacity=5)
    b = RedisTokenBucket("redis://unused", 600, client=shared, capacity=5)
    waits = [await a._try(1) for _ in range(3)] + [await b._try(1) for _ in range(2)]
    assert waits == [0.0] * 5
    assert await b._try(1) > 0, "the burst is spent across both processes"
    assert await a._try(1) > 0


async def test_refills_at_the_configured_rate():
    bucket = RedisTokenBucket("redis://unused", 6000, client=client(), capacity=1)   # 100/s
    assert await bucket._try(1) == 0.0
    wait = await bucket._try(1)
    assert 0 < wait <= 0.011
    await asyncio.sleep(0.03)
    assert await bucket._try(1) == 0.0


async def test_acquire_paces_callers():
    bucket = RedisTokenBucket("redis://unused", 1200, client=client(), capacity=1)   # 20/s
    start = time.perf_counter()
    for _ in range(4):
        await bucket.acquire()
    assert time.perf_counter() - start >= 0.14          # 3 waits of ~50ms


async def test_penalty_slows_every_process():
    shared = client()
    a = RedisTokenBucket("redis://unused", 600, client=shared, capacity=1)     # 10/s
    b = RedisTokenBucket("redis://unused", 600, client=shared, capacity=1)
    t = 1_000.0
    assert await b._try(1, now=t) == 0.0
    assert await b._try(1, now=t) == pytest.approx(0.1)
    a.penalize(0.75, 60.0)                               # another process sees a 429
    await asyncio.sleep(0.01)                            # the penalty write is a task
    assert float(await shared.get("ratelimit:jina:penalty")) == pytest.approx(0.25)
    assert await b._try(1, now=t) == pytest.approx(0.4)  # a quarter of the rate


async def test_falls_back_to_a_local_bucket_when_redis_is_down():
    class Broken:
        def register_script(self, _):
            async def boom(**kw):
                raise ConnectionError("redis is down")
            return boom

    bucket = RedisTokenBucket("redis://unused", 600, client=Broken(), capacity=2)
    await asyncio.wait_for(bucket.acquire(), 1)          # does not hang or raise
    await asyncio.wait_for(bucket.acquire(), 1)


def test_build_gate_uses_redis_only_when_asked(monkeypatch):
    from app.pipeline import fetch
    from app.settings import settings

    monkeypatch.setattr(settings, "fetch_backend", "jina")
    monkeypatch.setattr(settings, "rate_limit_backend", "local")
    assert not isinstance(fetch.build_gate().bucket, RedisTokenBucket)
    monkeypatch.setattr(settings, "rate_limit_backend", "redis")
    assert isinstance(fetch.build_gate().bucket, RedisTokenBucket)
