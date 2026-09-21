"""The multi-user queue against a real Postgres.

Skipped unless TEST_DATABASE_URL points at a database this suite may wipe, e.g.
    TEST_DATABASE_URL=postgresql+asyncpg://app:app@localhost:5432/extractor_test
The pipeline itself is replaced by a stub: these tests are about who gets served,
how often a domain is fetched, and what survives a crash -- not about extraction.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module", autouse=True)
def migrated():
    env = {**os.environ, "DATABASE_URL": TEST_DB}
    for args in (["downgrade", "base"], ["upgrade", "head"]):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
                       check=True, capture_output=True)


@pytest.fixture(autouse=True)
async def db(monkeypatch):
    from app import db as db_module
    from app.settings import settings

    monkeypatch.setattr(settings, "database_url", TEST_DB)
    async with db_module.get_sessionmaker()() as s:
        await s.execute(text("TRUNCATE jobs, job_items, job_domains, domains, pages, users CASCADE"))
        await s.commit()
    yield db_module.get_sessionmaker()
    await db_module.dispose_engine()


# --- helpers ---------------------------------------------------------------


async def make_user(sessions, name: str, admin: bool = False) -> tuple[uuid.UUID, str]:
    from app.auth import hash_key, new_key
    from app.models import User

    key = new_key()
    uid = uuid.uuid4()
    async with sessions() as s:
        s.add(User(id=uid, name=name, key_hash=hash_key(key), is_admin=admin))
        await s.commit()
    return uid, key


async def make_job(sessions, domains: list[str], owner=None, **kw):
    from app.jobs import create_job, prepare_plain

    async with sessions() as s:
        job, to_run, cached = await create_job(
            s, prepare_plain(domains), source_columns=["website"], owner_id=owner, **kw
        )
    return job


def stub_pipeline(monkeypatch, calls: list[str], *, status="found", delay=0.0, reason=None):
    from app import worker as worker_mod
    from app.schemas import DomainResult

    async def fake(domain, fetcher=None):
        calls.append(domain)
        if delay:
            await asyncio.sleep(delay)
        return DomainResult(domain=domain, status=status, error_reason=reason,
                            best_email=f"info@{domain}" if status == "found" else None,
                            jina_tokens=100)

    monkeypatch.setattr(worker_mod, "process_domain", fake)


async def run_until(sessions, predicate, *, concurrency=4, timeout=20.0):
    from app.worker import Worker

    w = Worker(concurrency=concurrency, fetcher=object())
    task = asyncio.create_task(w.run())
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            async with sessions() as s:
                if await predicate(s):
                    return w
            await asyncio.sleep(0.2)
        raise AssertionError("condition not reached before timeout")
    finally:
        w.stop()
        await task


async def job_status(s, job_id) -> str:
    return await s.scalar(text("SELECT status FROM jobs WHERE id = :j"), {"j": job_id})


# --- fairness --------------------------------------------------------------


async def test_a_small_job_is_served_beside_a_big_one(db):
    from app import queue

    big_owner, _ = await make_user(db, "big")
    small_owner, _ = await make_user(db, "small")
    big = await make_job(db, [f"big{i}.com" for i in range(200)], owner=big_owner)
    small = await make_job(db, ["s1.com", "s2.com", "s3.com"], owner=small_owner)

    order = []
    async with db() as s:
        for _ in range(6):
            c = await queue.claim(s, "w1")          # claimed, never finished: all in flight
            order.append(c.job_id)
    assert order.count(small.id) == 3, "the 3-row job must not wait behind 200 rows"
    assert order.count(big.id) == 3


async def test_one_user_with_two_jobs_does_not_get_double_the_share(db):
    from app import queue

    u1, _ = await make_user(db, "u1")
    u2, _ = await make_user(db, "u2")
    a1 = await make_job(db, [f"a{i}.com" for i in range(50)], owner=u1)
    a2 = await make_job(db, [f"b{i}.com" for i in range(50)], owner=u1)
    b = await make_job(db, [f"c{i}.com" for i in range(50)], owner=u2)

    got = []
    async with db() as s:
        for _ in range(8):
            got.append((await queue.claim(s, "w1")).job_id)
    assert got.count(b.id) == 4
    assert got.count(a1.id) + got.count(a2.id) == 4
    assert got.count(a1.id) == 2 and got.count(a2.id) == 2


# --- end to end with the worker ------------------------------------------


async def test_worker_finishes_a_job_and_results_stream_in_order(db, monkeypatch):
    from app.jobs import job_counts, stream_rows
    from app.models import Job

    calls: list[str] = []
    stub_pipeline(monkeypatch, calls)
    job = await make_job(db, ["a.com", "b.com", "https://a.com/contact", "not a url"])

    async def done(s):
        return await job_status(s, job.id) == "done"

    await run_until(db, done)
    assert sorted(calls) == ["a.com", "b.com"]      # deduped within the job

    async with db() as s:
        counts = await job_counts(s, job.id)
        assert counts["unique_domains"] == 2 and counts["done"] == 2 and counts["found"] == 2
        assert counts["jina_tokens"] == 200
        j = await s.get(Job, job.id)
        rows = [r async for r in stream_rows(s, j)]
    assert [r[1].status if r[1] else None for r in rows] == [
        "found", "found", "found", "invalid_input",
    ]
    assert rows[2][1].best_email == "info@a.com"


async def test_the_same_domain_in_two_live_jobs_is_fetched_once(db, monkeypatch):
    calls: list[str] = []
    stub_pipeline(monkeypatch, calls, delay=0.5)
    u1, _ = await make_user(db, "u1")
    u2, _ = await make_user(db, "u2")
    j1 = await make_job(db, ["shared.com", "one.com"], owner=u1)
    j2 = await make_job(db, ["shared.com", "two.com"], owner=u2)

    async def both_done(s):
        return (await job_status(s, j1.id) == "done") and (await job_status(s, j2.id) == "done")

    from app import queue
    monkeypatch.setattr(queue, "LOCK_BUSY_DELAY_S", 0.2)
    await run_until(db, both_done)
    assert calls.count("shared.com") == 1
    async with db() as s:
        from_cache = await s.scalar(text(
            "SELECT count(*) FROM job_domains WHERE domain = 'shared.com' AND from_cache"))
        tokens = await s.scalar(text(
            "SELECT sum(jina_tokens) FROM job_domains WHERE domain = 'shared.com'"))
    assert from_cache == 1 and tokens == 100      # only the job that fetched it pays


async def test_cache_is_reused_unless_the_job_asks_for_fresh(db, monkeypatch):
    calls: list[str] = []
    stub_pipeline(monkeypatch, calls)
    first = await make_job(db, ["c.com"])

    async def done(job):
        async def pred(s):
            return await job_status(s, job.id) == "done"
        return pred

    await run_until(db, await done(first))
    second = await make_job(db, ["c.com"])
    await run_until(db, await done(second))
    assert calls == ["c.com"]

    third = await make_job(db, ["c.com"], fresh=True)
    await run_until(db, await done(third))
    assert calls == ["c.com", "c.com"]


async def test_an_all_invalid_upload_is_done_immediately(db):
    job = await make_job(db, ["not a url", "facebook.com/acme"])
    async with db() as s:
        assert await job_status(s, job.id) == "done"


# --- failure handling ------------------------------------------------------


async def test_provider_account_error_pauses_every_job_and_marks_nothing(db, monkeypatch):
    from app import worker as worker_mod
    from app.pipeline.fetch import ErrorClass, FetchError

    async def dead_key(domain, fetcher=None):
        raise FetchError(ErrorClass.PROVIDER_ACCOUNT, "provider_account", detail="402")

    monkeypatch.setattr(worker_mod, "process_domain", dead_key)
    u1, _ = await make_user(db, "u1")
    u2, _ = await make_user(db, "u2")
    j1 = await make_job(db, ["x1.com", "x2.com"], owner=u1)
    j2 = await make_job(db, ["y1.com"], owner=u2)

    async def paused(s):
        return (await job_status(s, j1.id) == "paused") and (await job_status(s, j2.id) == "paused")

    await run_until(db, paused)
    async with db() as s:
        done = await s.scalar(text("SELECT count(*) FROM job_domains WHERE state = 'done'"))
        running = await s.scalar(text("SELECT count(*) FROM job_domains WHERE state = 'running'"))
        attempts = await s.scalar(text("SELECT max(attempts) FROM job_domains"))
        locks = await s.scalar(text("SELECT count(*) FROM domains WHERE lock_owner IS NOT NULL"))
        reason = await s.scalar(text("SELECT pause_reason FROM jobs WHERE id = :j"), {"j": j1.id})
    assert done == 0 and running == 0 and locks == 0
    assert attempts == 0, "a dead key must not use up the domains' attempts"
    assert reason.endswith("_account")


async def test_a_worker_that_dies_has_its_rows_reaped(db):
    from app import queue

    job = await make_job(db, ["dies.com"])
    async with db() as s:
        c = await queue.claim(s, "dead-worker")
        assert await queue.lock_domain(s, c.domain, queue.lock_token("dead-worker", c))
        assert await queue.reap(s) == 0                         # still fresh
        await s.execute(text(
            "UPDATE job_domains SET heartbeat_at = now() - interval '10 minutes'"))
        await s.execute(text("UPDATE domains SET lock_until = now() - interval '1 minute'"))
        await s.commit()
        assert await queue.reap(s) == 1
        again = await queue.claim(s, "new-worker")
        assert again.domain == "dies.com" and again.attempts == 2
        # the expired lock is free to the new claim
        assert await queue.lock_domain(s, "dies.com", queue.lock_token("new-worker", again))


async def test_a_clean_shutdown_hands_work_back_without_spending_an_attempt(db, monkeypatch):
    from app.worker import Worker

    calls: list[str] = []
    stub_pipeline(monkeypatch, calls, delay=30)
    job = await make_job(db, ["slow.com"])
    w = Worker(concurrency=2, fetcher=object())
    task = asyncio.create_task(w.run())
    for _ in range(50):
        await asyncio.sleep(0.1)
        if calls:
            break
    w.stop()
    await asyncio.wait_for(task, 10)
    async with db() as s:
        row = (await s.execute(text(
            "SELECT state, attempts FROM job_domains WHERE job_id = :j"), {"j": job.id})).one()
        lock = await s.scalar(text("SELECT lock_owner FROM domains WHERE domain = 'slow.com'"))
    assert row == ("queued", 0) and lock is None


async def test_a_worker_cannot_lock_a_domain_twice_for_two_jobs(db):
    from app import queue

    await make_job(db, ["twice.com"])
    await make_job(db, ["twice.com"])
    async with db() as s:
        c1 = await queue.claim(s, "w")
        c2 = await queue.claim(s, "w")
        assert c1.job_id != c2.job_id
        assert await queue.lock_domain(s, "twice.com", queue.lock_token("w", c1))
        assert not await queue.lock_domain(s, "twice.com", queue.lock_token("w", c2))
        # and the loser cannot release the winner's lock
        await queue.unlock_domain(s, "twice.com", queue.lock_token("w", c2))
        owner = await s.scalar(text("SELECT lock_owner FROM domains WHERE domain = 'twice.com'"))
        assert owner == queue.lock_token("w", c1)


async def test_circuit_open_goes_to_the_retry_pass_and_shows_as_retrying(db, monkeypatch):
    from app.jobs import stream_rows
    from app.models import Job
    from app.settings import settings

    calls: list[str] = []
    stub_pipeline(monkeypatch, calls, status="fetch_failed", reason="circuit_open")
    monkeypatch.setattr(settings, "retry_pass_delay_s", 3600)
    job = await make_job(db, ["flaky.com"])

    async def requeued(s):
        return await s.scalar(text(
            "SELECT state = 'queued' AND attempts = 1 FROM job_domains WHERE job_id = :j"),
            {"j": job.id})

    await run_until(db, requeued)
    async with db() as s:
        j = await s.get(Job, job.id)
        rows = [r async for r in stream_rows(s, j)]
        assert await job_status(s, job.id) == "running"
    assert rows[0][1] is None and rows[0][3] is True


async def test_webhook_gets_each_result_with_its_row_indexes(db, monkeypatch):
    from app import worker as worker_mod

    calls: list[str] = []
    sent: list[tuple[str, dict]] = []
    stub_pipeline(monkeypatch, calls)

    async def capture(url, payload, **kw):
        sent.append((url, payload))
        return True

    monkeypatch.setattr(worker_mod, "deliver", capture)
    job = await make_job(db, ["w.com", "v.com", "www.w.com"], webhook_url="https://hook.test/x")

    async def done(s):
        return await job_status(s, job.id) == "done"

    await run_until(db, done)
    await asyncio.sleep(0.1)
    by_domain = {p["result"]["domain"]: p for _, p in sent}
    assert set(by_domain) == {"w.com", "v.com"}
    assert by_domain["w.com"]["row_indexes"] == [0, 2]
    assert by_domain["w.com"]["job_id"] == str(job.id)


# --- API: keys and ownership ----------------------------------------------


async def test_users_only_see_their_own_jobs(db, api_key):
    import httpx

    from app.main import app

    _, alice = await make_user(db, "alice")
    _, bob = await make_user(db, "bob")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/jobs", json={"items": ["alice.com"]}, headers={"X-API-Key": alice})
        assert r.status_code == 200, r.text
        jid = r.json()["job_id"]

        assert (await c.get(f"/jobs/{jid}", headers={"X-API-Key": alice})).status_code == 200
        assert (await c.get(f"/jobs/{jid}", headers={"X-API-Key": bob})).status_code == 404
        assert (await c.get(f"/jobs/{jid}/results.csv",
                            headers={"X-API-Key": bob})).status_code == 404
        assert (await c.post(f"/jobs/{jid}/cancel",
                             headers={"X-API-Key": bob})).status_code == 404
        assert (await c.get(f"/jobs/{jid}", headers={"X-API-Key": api_key})).status_code == 200

        mine = (await c.get("/jobs", headers={"X-API-Key": alice})).json()
        theirs = (await c.get("/jobs", headers={"X-API-Key": bob})).json()
        assert mine["user"] == "alice" and [j["job_id"] for j in mine["jobs"]] == [jid]
        assert theirs["jobs"] == []

        bad = await c.get("/jobs", headers={"X-API-Key": "ee_not-a-real-key"})
        assert bad.status_code == 401


async def test_revoked_key_is_rejected(db):
    import httpx

    from app.main import app

    _, key = await make_user(db, "gone")
    async with db() as s:
        await s.execute(text("UPDATE users SET revoked_at = now()"))
        await s.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/jobs", headers={"X-API-Key": key})).status_code == 401


async def test_active_job_limit_per_user(db, monkeypatch):
    import httpx

    from app.main import app
    from app.settings import settings

    monkeypatch.setattr(settings, "max_active_jobs_per_user", 2)
    _, key = await make_user(db, "busy")
    h = {"X-API-Key": key}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        ids = []
        for i in range(2):
            r = await c.post("/jobs", json={"items": [f"d{i}.com"]}, headers=h)
            assert r.status_code == 200
            ids.append(r.json()["job_id"])
        assert (await c.post("/jobs", json={"items": ["d9.com"]}, headers=h)).status_code == 429
        assert (await c.post(f"/jobs/{ids[0]}/cancel", headers=h)).status_code == 200
        assert (await c.post("/jobs", json={"items": ["d9.com"]}, headers=h)).status_code == 200


async def test_cancelled_jobs_are_not_claimed(db):
    from app import queue

    job = await make_job(db, ["gone1.com", "gone2.com"])
    async with db() as s:
        await s.execute(text("UPDATE jobs SET status = 'failed', pause_reason = 'cancelled'"))
        await s.commit()
        assert await queue.claim(s, "w") is None


async def test_resume_completes_a_job_that_finished_while_paused(db, monkeypatch):
    import httpx

    from app.main import app
    from app.settings import settings

    job = await make_job(db, ["p.com"])
    async with db() as s:
        await s.execute(text("UPDATE job_domains SET state = 'done'"))
        await s.execute(text("UPDATE jobs SET status = 'paused', pause_reason = 'jina_account'"))
        await s.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(f"/jobs/{job.id}/resume", headers={"X-API-Key": settings.api_key})
    assert r.json()["status"] == "done"


def test_cache_usable_rules():
    from types import SimpleNamespace

    from app.queue import cache_usable

    now = datetime.now(UTC)
    job = SimpleNamespace(created_at=now, fresh=False)
    fresh_job = SimpleNamespace(created_at=now, fresh=True)
    r = {"domain": "a.com"}
    assert cache_usable(job, now + timedelta(seconds=1), "fetch_failed", r)   # just finished
    assert cache_usable(job, now - timedelta(days=1), "found", r)
    assert not cache_usable(job, now - timedelta(days=1), "fetch_failed", r)
    assert not cache_usable(job, now - timedelta(days=400), "found", r)
    assert not cache_usable(fresh_job, now - timedelta(days=1), "found", r)
    assert cache_usable(fresh_job, now + timedelta(seconds=1), "found", r)
    assert not cache_usable(job, None, None, None)
