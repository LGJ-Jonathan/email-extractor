"""The multi-user queue against a real Postgres.

Skipped unless TEST_DATABASE_URL points at a database this suite may wipe, e.g.
    TEST_DATABASE_URL=postgresql+asyncpg://app:app@localhost:5432/extractor_test
The pipeline itself is replaced by a stub: these tests are about who gets served,
how often a domain is fetched, and what survives a crash -- not about extraction.
"""

import asyncio
import json
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
        await s.execute(text("TRUNCATE jobs, job_items, job_domains, domains, pages, users, provider_keys, provider_key_audit, process_status CASCADE"))
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
        raise AssertionError(f"condition not reached before timeout; worker: {task}")
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
        assert await queue.lock_domain(s, c)
        assert await queue.reap(s) == 0                         # still fresh
        await s.execute(text(
            "UPDATE job_domains SET heartbeat_at = now() - interval '10 minutes'"))
        await s.execute(text("UPDATE domains SET lock_until = now() - interval '1 minute'"))
        await s.commit()
        assert await queue.reap(s) == 1
        again = await queue.claim(s, "new-worker")
        assert again.domain == "dies.com" and again.attempts == 2
        # the expired lock is free to the new claim
        assert await queue.lock_domain(s, again)


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
        assert await queue.lock_domain(s, c1)
        assert not await queue.lock_domain(s, c2)
        # and the loser cannot release the winner's lock
        await queue.unlock_domain(s, c2)
        owner = await s.scalar(text("SELECT lock_owner FROM domains WHERE domain = 'twice.com'"))
        assert owner == c1.token


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
    from app import webhook

    calls: list[str] = []
    sent: list[tuple[str, dict]] = []
    stub_pipeline(monkeypatch, calls)

    async def capture(url, payload, **kw):
        sent.append((url, payload))
        return True

    monkeypatch.setattr(webhook, "deliver", capture)
    job = await make_job(db, ["w.com", "v.com", "www.w.com"], webhook_url="https://hook.test/x")
    # (make_job skips the API's URL check; the address check is tested separately)

    async def done(s):
        return await job_status(s, job.id) == "done"

    await run_until(db, done)
    by_domain = {p["result"]["domain"]: p for _, p in sent if p["event"] == "domain.done"}
    assert set(by_domain) == {"w.com", "v.com"}
    assert by_domain["w.com"]["row_indexes"] == [0, 2]
    assert by_domain["w.com"]["job_id"] == str(job.id)
    finals = [p for _, p in sent if p["event"] == "job.done"]
    assert len(finals) == 1 and finals[0]["counts"]["done"] == 2
    assert len({p["delivery_id"] for _, p in sent}) == len(sent)


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
        await s.execute(text("UPDATE jobs SET status = 'cancelled'"))
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


# --- review fixes ----------------------------------------------------------


async def test_a_reaped_claim_cannot_finish_the_row_again(db):
    """A stale handler finishing after its row was reaped and reclaimed is a no-op."""
    from app import queue
    from app.schemas import DomainResult

    await make_job(db, ["stale.com"])
    async with db() as s:
        old = await queue.claim(s, "w")
        await s.execute(text("UPDATE job_domains SET heartbeat_at = now() - interval '1 hour'"))
        await s.commit()
        await queue.reap(s)
        new = await queue.claim(s, "w")                   # same worker, new claim
        assert new.token != old.token
        r = DomainResult(domain="stale.com", status="found", best_email="a@stale.com")
        assert await queue.finish(s, old, r, from_cache=False) == (False, False)
        assert await s.scalar(text("SELECT state FROM job_domains")) == "running"
        assert await s.scalar(text("SELECT finished_at FROM domains")) is None
        recorded, done = await queue.finish(s, new, r, from_cache=False)
        assert recorded and done


async def test_too_many_attempts_does_not_poison_the_shared_cache(db, monkeypatch):
    from app import worker as worker_mod
    from app.schemas import DomainResult

    async with db() as s:
        good = DomainResult(domain="good.com", status="found", best_email="info@good.com")
        await s.execute(text("""
            INSERT INTO domains (domain, stage, status, result, finished_at)
            VALUES ('good.com', 'done', 'found', CAST(:r AS jsonb), now() - interval '1 day')
        """), {"r": good.model_dump_json()})
        await s.commit()
    job = await make_job(db, ["good.com"], fresh=True)
    async with db() as s:
        await s.execute(text("UPDATE job_domains SET attempts = 99"))
        await s.commit()
    monkeypatch.setattr(worker_mod, "process_domain", None)       # must not be reached

    async def done(s):
        return await job_status(s, job.id) == "done"

    await run_until(db, done)
    async with db() as s:
        shared = await s.scalar(text("SELECT status FROM domains WHERE domain = 'good.com'"))
        mine = await s.scalar(text("SELECT status FROM job_domains"))
    assert shared == "found" and mine == "fetch_failed"


async def test_a_finished_jobs_download_does_not_change_when_the_cache_does(db, monkeypatch):
    from app.jobs import job_counts, stream_rows
    from app.models import Job

    calls: list[str] = []
    stub_pipeline(monkeypatch, calls)
    first = await make_job(db, ["moving.com"])

    async def done(job):
        async def pred(s):
            return await job_status(s, job.id) == "done"
        return pred

    await run_until(db, await done(first))
    stub_pipeline(monkeypatch, calls, status="no_contact_info")
    second = await make_job(db, ["moving.com"], fresh=True)
    await run_until(db, await done(second))

    async with db() as s:
        j = await s.get(Job, first.id)
        rows = [r async for r in stream_rows(s, j)]
        counts = await job_counts(s, first.id)
    assert rows[0][1].status == "found" and counts["found"] == 1


async def test_concurrent_job_creation_respects_the_limit(db, monkeypatch):
    import httpx

    from app.main import app
    from app.settings import settings

    monkeypatch.setattr(settings, "max_active_jobs_per_user", 1)
    _, key = await make_user(db, "racer")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        rs = await asyncio.gather(*[
            c.post("/jobs", json={"items": [f"r{i}.com"]}, headers={"X-API-Key": key})
            for i in range(5)
        ])
    assert sorted(r.status_code for r in rs) == [200, 429, 429, 429, 429]


async def test_cancel_and_resume_only_move_from_the_right_states(db, api_key):
    import httpx

    from app.main import app

    job = await make_job(db, ["st.com"])
    h = {"X-API-Key": api_key}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post(f"/jobs/{job.id}/resume", headers=h)).status_code == 409
        r = await c.post(f"/jobs/{job.id}/cancel", headers=h)
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        again = await c.post(f"/jobs/{job.id}/cancel", headers=h)
        assert again.status_code == 409 and "cancelled" in again.json()["detail"]
        assert (await c.post(f"/jobs/{job.id}/resume", headers=h)).status_code == 409


async def test_downloads_do_not_leak_connections(db, api_key):
    import httpx

    from app.main import app

    job = await make_job(db, ["leak.com", "leak2.com"])
    h = {"X-API-Key": api_key}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for _ in range(5):
            assert (await c.get(f"/jobs/{job.id}/results.csv", headers=h)).status_code == 200
            assert (await c.get(f"/jobs/{job.id}/results.json", headers=h)).status_code == 200
    async with db() as s:
        stuck = await s.scalar(text(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND state = 'idle in transaction'"))
    assert stuck == 0


async def test_private_webhook_urls_are_refused_at_submit(db, api_key):
    import httpx

    from app.main import app

    h = {"X-API-Key": api_key}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for url in ("https://127.0.0.1/x", "https://169.254.169.254/latest",
                    "https://10.0.0.5/hook", "https://localhost/x", "http://example.com/x",
                    "https://user:pw@example.com/x", "ftp://example.com/x"):
            r = await c.post("/jobs", json={"items": ["a.com"], "webhook_url": url}, headers=h)
            assert r.status_code == 422, url


# --- service key + acting user -------------------------------------------


async def make_service(sessions, name="portal"):
    from app.auth import hash_key, new_key
    from app.models import User

    key = new_key()
    async with sessions() as s:
        s.add(User(id=uuid.uuid4(), name=name, key_hash=hash_key(key), is_service=True))
        await s.commit()
    return key


async def test_service_key_acts_for_each_person_separately(db):
    import httpx

    from app.main import app

    svc = await make_service(db)
    a = {"X-API-Key": svc, "X-Acting-User": "Alice@LeadGenJay.com"}
    b = {"X-API-Key": svc, "X-Acting-User": "bob@leadgenjay.com"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        jid = (await c.post("/jobs", json={"items": ["x.com"]}, headers=a)).json()["job_id"]
        assert (await c.get(f"/jobs/{jid}", headers=a)).status_code == 200
        r = await c.get(f"/jobs/{jid}", headers=b)
        assert r.status_code == 404 and r.json()["error"]["code"] == "job_not_found"
        mine = (await c.get("/jobs", headers=a)).json()
        assert mine["user"] == "alice@leadgenjay.com" and len(mine["jobs"]) == 1
        admin = {**b, "X-Acting-Role": "admin"}
        assert (await c.get(f"/jobs/{jid}", headers=admin)).status_code == 200

        r = await c.get("/jobs", headers={"X-API-Key": svc})
        assert r.status_code == 400 and r.json()["error"]["code"] == "acting_user_required"
        r = await c.get("/jobs", headers={"X-API-Key": svc, "X-Acting-User": "not-an-email"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_acting_user"
    async with db() as s:
        owner = await s.scalar(text(
            "SELECT u.name FROM jobs j JOIN users u ON u.id = j.owner_id"))
    assert owner == "alice@leadgenjay.com"


async def test_the_job_limit_applies_per_acting_person_not_per_service_key(db, monkeypatch):
    import httpx

    from app.main import app
    from app.settings import settings

    monkeypatch.setattr(settings, "max_active_jobs_per_user", 1)
    svc = await make_service(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for who in ("a@x.com", "b@x.com"):
            r = await c.post("/jobs", json={"items": ["q.com"]},
                             headers={"X-API-Key": svc, "X-Acting-User": who})
            assert r.status_code == 200
        r = await c.post("/jobs", json={"items": ["q.com"]},
                         headers={"X-API-Key": svc, "X-Acting-User": "a@x.com"})
        assert r.status_code == 429 and r.json()["error"]["code"] == "job_limit_reached"


async def test_a_personal_key_cannot_claim_to_act_for_someone(db):
    import httpx

    from app.main import app

    _, key = await make_user(db, "sam")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/jobs", headers={"X-API-Key": key, "X-Acting-User": "boss@x.com"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "acting_user_not_allowed"
        r = await c.get("/jobs", headers={"X-API-Key": key, "X-Acting-Role": "admin"})
        assert r.status_code == 403


async def test_a_removed_person_is_refused(db):
    import httpx

    from app.main import app

    svc = await make_service(db)
    h = {"X-API-Key": svc, "X-Acting-User": "gone@x.com"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/jobs", headers=h)).status_code == 200
        async with db() as s:
            await s.execute(text("UPDATE users SET revoked_at = now() WHERE name = 'gone@x.com'"))
            await s.commit()
        r = await c.get("/jobs", headers=h)
        assert r.status_code == 403 and r.json()["error"]["code"] == "acting_user_revoked"


async def test_rate_limit_per_person_exempts_polling(db, monkeypatch):
    import fakeredis
    import httpx

    from app import auth
    from app.main import app
    from app.settings import settings

    monkeypatch.setattr(auth, "_redis", fakeredis.FakeAsyncRedis())
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)
    svc = await make_service(db)
    a = {"X-API-Key": svc, "X-Acting-User": "a@x.com"}
    b = {"X-API-Key": svc, "X-Acting-User": "b@x.com"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for _ in range(3):
            assert (await c.post("/extract", json={"url": "Not A Url"}, headers=a)).status_code == 200
        r = await c.post("/extract", json={"url": "Not A Url"}, headers=a)
        assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
        assert int(r.headers["retry-after"]) >= 1
        assert (await c.get("/jobs", headers=a)).status_code == 200          # polling exempt
        assert (await c.post("/extract", json={"url": "Not A Url"}, headers=b)).status_code == 200


# --- data for the job screens ---------------------------------------------


async def test_job_detail_has_the_breakdown_the_page_shows(db, monkeypatch, api_key):
    import httpx

    from app import worker as worker_mod
    from app.main import app
    from app.schemas import DomainResult

    outcomes = {
        "a.com": DomainResult(domain="a.com", status="found", best_email="x@a.com",
                              needs_review=True, phones=["+15550000000"],
                              socials={"linkedin": "https://linkedin.com/company/a"},
                              typesafe_called=True, jina_tokens=300,
                              pages_fetched=["https://a.com/", "https://a.com/contact"]),
        "b.com": DomainResult(domain="b.com", status="form_only",
                              contact_form_url="https://b.com/contact", jina_tokens=100),
        "c.com": DomainResult(domain="c.com", status="fetch_failed", error_reason="blocked"),
    }

    async def fake(domain, fetcher=None):
        return outcomes[domain]

    monkeypatch.setattr(worker_mod, "process_domain", fake)
    job = await make_job(db, ["a.com", "b.com", "c.com", "", "not a url"])

    async def done(s):
        return await job_status(s, job.id) == "done"

    await run_until(db, done)
    h = {"X-API-Key": api_key}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        d = (await c.get(f"/jobs/{job.id}", headers=h)).json()
        recent = (await c.get(f"/jobs/{job.id}/recent?limit=2", headers=h)).json()["domains"]
    assert d["by_status"] == {"found": 1, "form_only": 1, "fetch_failed": 1}
    assert d["needs_review"] == 1 and d["typesafe_calls"] == 1
    assert d["captured"] == {"phones": 1, "contact_forms": 1, "linkedin": 1}
    assert d["invalid_rows"] == 2 and d["empty_rows"] == 1 and d["total"] == 5
    assert d["tokens_per_domain"] == 200 and d["paused_at"] is None
    assert len(recent) == 2 and {"domain", "status", "best_email", "pages"} <= set(recent[0])
    a = next((r for r in recent if r["domain"] == "a.com"), None)
    if a:
        assert a["pages"] == 2 and a["needs_review"] is True


async def test_pausing_records_when_and_resuming_clears_it(db, api_key):
    import httpx

    from app import queue
    from app.main import app

    job = await make_job(db, ["p1.com"])
    async with db() as s:
        await queue.pause_active_jobs(s, "jina_account")
    h = {"X-API-Key": api_key}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get(f"/jobs/{job.id}", headers=h)).json()["paused_at"]
        await c.post(f"/jobs/{job.id}/resume", headers=h)
        assert (await c.get(f"/jobs/{job.id}", headers=h)).json()["paused_at"] is None


# --- second review pass ----------------------------------------------------


async def test_a_retried_create_with_the_same_idempotency_key_returns_the_first_job(db):
    import httpx

    from app.main import app

    svc = await make_service(db)
    h = {"X-API-Key": svc, "X-Acting-User": "a@x.com", "Idempotency-Key": "upload-123"}
    other = {"X-API-Key": svc, "X-Acting-User": "b@x.com", "Idempotency-Key": "upload-123"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        rs = await asyncio.gather(*[
            c.post("/jobs", json={"items": ["i1.com", "i2.com"]}, headers=h) for _ in range(3)
        ])
        assert all(r.status_code == 200 for r in rs)
        assert len({r.json()["job_id"] for r in rs}) == 1
        assert sum(1 for r in rs if r.json().get("replayed")) == 2
        # the same key from someone else is a different job
        r = await c.post("/jobs", json={"items": ["i1.com"]}, headers=other)
        assert r.json()["job_id"] != rs[0].json()["job_id"]
    async with db() as s:
        assert await s.scalar(text("SELECT count(*) FROM jobs")) == 2


async def test_active_time_excludes_pauses(db, api_key):
    import httpx

    from app import queue
    from app.main import app

    job = await make_job(db, ["t1.com", "t2.com"])
    async with db() as s:
        await queue.claim(s, "w")                       # sets started_at
        await s.execute(text(
            "UPDATE jobs SET started_at = now() - interval '2 hours', "
            "paused_seconds = 3600"))
        await s.commit()
    h = {"X-API-Key": api_key}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        d = (await c.get(f"/jobs/{job.id}", headers=h)).json()
        assert 3590 < d["active_seconds"] < 3700
        assert d["columns"] == ["website"]
        async with db() as s:
            await queue.pause_active_jobs(s, "jina_account")
            await s.execute(text("UPDATE jobs SET paused_at = now() - interval '30 minutes'"))
            await s.commit()
        d = (await c.get(f"/jobs/{job.id}", headers=h)).json()
        assert 1790 < d["active_seconds"] < 1900          # the running pause is excluded
        await c.post(f"/jobs/{job.id}/resume", headers=h)
    async with db() as s:
        assert 5390 < await s.scalar(text("SELECT paused_seconds FROM jobs")) < 5500


async def test_admin_job_list_names_the_owner(db, api_key):
    import httpx

    from app.main import app

    uid, _ = await make_user(db, "sam")
    await make_job(db, ["o1.com"], owner=uid)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        body = (await c.get("/jobs", headers={"X-API-Key": api_key})).json()
    assert body["all_users"] is True and body["jobs"][0]["owner"] == "sam"


async def test_worker_stops_promptly_when_every_slot_is_busy(db, monkeypatch):
    from app.worker import Worker

    calls: list[str] = []
    stub_pipeline(monkeypatch, calls, delay=60)
    await make_job(db, [f"busy{i}.com" for i in range(3)])
    w = Worker(concurrency=2, fetcher=object())
    task = asyncio.create_task(w.run())
    for _ in range(50):
        await asyncio.sleep(0.1)
        if len(calls) >= 2:
            break
    assert len(calls) == 2                               # both slots busy, one queued
    w.stop()
    await asyncio.wait_for(task, 5)                      # not the 60 s a domain takes
    async with db() as s:
        running = await s.scalar(text("SELECT count(*) FROM job_domains WHERE state='running'"))
    assert running == 0 and len(calls) == 2              # nothing new claimed after stop


# --- third review pass ----------------------------------------------------


async def test_reusing_a_key_for_a_different_request_is_409(db):
    import httpx

    from app.main import app

    svc = await make_service(db)
    h = {"X-API-Key": svc, "X-Acting-User": "k@x.com", "Idempotency-Key": "same-key-1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/jobs", json={"items": ["a.com"]}, headers=h)).status_code == 200
        r = await c.post("/jobs", json={"items": ["a.com"], "fresh": True}, headers=h)
        assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_conflict"
        r = await c.post("/jobs", json={"items": ["a.com"]}, headers=h)
        assert r.status_code == 200 and r.json()["replayed"] is True


async def test_cancelled_jobs_stop_accruing_active_time(db, api_key):
    import httpx

    from app import queue
    from app.main import app

    job = await make_job(db, ["c1.com", "c2.com"])
    async with db() as s:
        await queue.claim(s, "w")
        await s.execute(text("UPDATE jobs SET started_at = now() - interval '1 hour'"))
        await s.commit()
    h = {"X-API-Key": api_key}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post(f"/jobs/{job.id}/cancel", headers=h)
        first = (await c.get(f"/jobs/{job.id}", headers=h)).json()
        await asyncio.sleep(1.1)
        second = (await c.get(f"/jobs/{job.id}", headers=h)).json()
    assert first["finished_at"] and abs(second["active_seconds"] - first["active_seconds"]) < 0.5


async def test_a_pause_before_the_first_claim_is_not_subtracted(db, api_key):
    import httpx

    from app import queue
    from app.main import app

    job = await make_job(db, ["q1.com"])
    async with db() as s:
        await queue.pause_active_jobs(s, "jina_account")
        await s.execute(text("UPDATE jobs SET paused_at = now() - interval '8 hours'"))
        await s.commit()
    h = {"X-API-Key": api_key}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post(f"/jobs/{job.id}/resume", headers=h)
    async with db() as s:
        assert await s.scalar(text("SELECT paused_seconds FROM jobs")) == 0
        await queue.claim(s, "w")
        await s.execute(text("UPDATE jobs SET started_at = now() - interval '10 minutes'"))
        await s.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        d = (await c.get(f"/jobs/{job.id}", headers=h)).json()
    assert 590 < d["active_seconds"] < 700


async def test_one_person_one_row_whatever_the_case(db):
    import httpx

    from app.main import app

    svc = await make_service(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for who in ("Mixed@Case.com", "mixed@case.com", "MIXED@CASE.COM"):
            await c.get("/jobs", headers={"X-API-Key": svc, "X-Acting-User": who})
    async with db() as s:
        n = await s.scalar(text("SELECT count(*) FROM users WHERE lower(name) = 'mixed@case.com'"))
    assert n == 1


async def test_providers_status_endpoint_shape(db, api_key, monkeypatch):
    import httpx

    from app import providers
    from app.main import app

    async def fake_jina(client):
        return {"status": "low", "balance_tokens": 42}

    async def fake_ts(client):
        return {"status": "ok"}

    monkeypatch.setattr(providers, "_jina", fake_jina)
    monkeypatch.setattr(providers, "_typesafe", fake_ts)
    providers.reset_cache()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        body = (await c.get("/providers/status", headers={"X-API-Key": api_key})).json()
    providers.reset_cache()
    assert body["jina"] == {"status": "low", "balance_tokens": 42}
    assert set(body["usage_this_month"]) == {"jina_tokens", "typesafe_calls", "domains_fetched"}


# --- provider keys, estimates, big jobs ------------------------------------


async def test_admin_saves_a_checked_key_and_paused_jobs_resume(db, monkeypatch, api_key):
    import base64
    import os as _os

    import httpx

    from app import provider_keys, providers, queue
    from app.main import app
    from app.settings import settings

    monkeypatch.setattr(settings, "provider_key_encryption_key",
                        base64.b64encode(_os.urandom(32)).decode())
    provider_keys.reset_for_tests()

    async def fake_balance(client, key):
        return 5_000_000_000 if key == "jina_good_key_1234" else None

    monkeypatch.setattr(providers, "jina_balance", fake_balance)
    job = await make_job(db, ["r1.com"])
    async with db() as s:
        await queue.pause_active_jobs(s, "jina_account")

    svc = await make_service(db)
    admin = {"X-API-Key": svc, "X-Acting-User": "boss@x.com", "X-Acting-Role": "admin"}
    staff = {"X-API-Key": svc, "X-Acting-User": "staff@x.com"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.put("/providers/keys/jina", json={"key": "jina_good_key_1234"}, headers=staff)
        assert r.status_code == 403
        r = await c.put("/providers/keys/jina", json={"key": "jina_bad_key_0000"}, headers=admin)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_provider_key"
        r = await c.put("/providers/keys/jina", json={"key": "jina_good_key_1234"}, headers=admin)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["last4"] == "1234" and body["resumed_jobs"] == 1 and body["status"] == "ok"
        assert "jina_good_key_1234" not in r.text
        keys = (await c.get("/providers/keys", headers=admin)).json()
        assert keys["keys"]["jina"]["source"] == "portal" and keys["keys"]["jina"]["updated_by"] == "boss@x.com"
        assert "jina_good_key_1234" not in json.dumps(keys)
        assert (await c.put("/providers/keys/nope", json={"key": "x" * 12}, headers=admin)).status_code == 404
    async with db() as s:
        stored = await s.scalar(text("SELECT ciphertext FROM provider_keys WHERE provider='jina'"))
        assert "jina_good_key_1234" not in stored
        assert await job_status(s, job.id) == "running"
    assert provider_keys.current("jina") == "jina_good_key_1234"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.delete("/providers/keys/jina", headers=admin)
        assert r.status_code == 200
    assert provider_keys.source("jina") != "portal"
    provider_keys.reset_for_tests()


async def test_estimate_uses_the_typical_rate_until_there_is_data(db, api_key, monkeypatch):
    import httpx

    from app import providers
    from app.main import app

    async def fake_status(session):
        return {"jina": {"status": "ok", "balance_tokens": 1}, "typesafe": {"status": "ok"},
                "usage_this_month": {}, "jina_low_threshold": 1, "checked_at": ""}

    monkeypatch.setattr(providers, "provider_status", fake_status)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        e = (await c.get("/estimate?domains=400", headers={"X-API-Key": api_key})).json()
        assert e["basis"] == "typical" and e["people_running"] == 0
        assert e["eta_seconds"]["low"] < 600 < e["eta_seconds"]["high"]      # ~10 min at 40/min
        bad = await c.get("/estimate?domains=60000", headers={"X-API-Key": api_key})
        assert bad.status_code == 422


async def test_job_list_carries_progress(db, api_key):
    import httpx

    from app.main import app

    job = await make_job(db, ["p1.com", "p2.com", "p3.com"])
    async with db() as s:
        await s.execute(text("UPDATE job_domains SET state='done', status='found' "
                             "WHERE domain='p1.com'"))
        await s.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        j = (await c.get("/jobs", headers={"X-API-Key": api_key})).json()["jobs"][0]
    assert j["job_id"] == str(job.id)
    assert (j["done"], j["unique_domains"], j["found"]) == (1, 3, 1)


async def test_a_job_with_more_than_32767_domains_is_created(db):
    """asyncpg allows 32,767 parameters per statement; every large job used to fail."""
    job = await make_job(db, [f"domain{i}-shop.com" for i in range(40_000)])
    async with db() as s:
        n = await s.scalar(text("SELECT count(*) FROM job_domains WHERE job_id = :j"),
                           {"j": job.id})
    assert n == 40_000


# --- key store hardening ---------------------------------------------------


def _new_master(monkeypatch):
    import base64
    import os as _os

    from app.settings import settings

    monkeypatch.setattr(settings, "provider_key_encryption_key",
                        base64.b64encode(_os.urandom(32)).decode())


async def test_an_undecryptable_saved_key_fails_closed_not_to_the_env_key(db, monkeypatch):
    from app import provider_keys
    from app.settings import settings

    provider_keys.reset_for_tests()
    monkeypatch.setattr(settings, "jina_api_key", "old_env_key_that_leaked")
    _new_master(monkeypatch)
    async with db() as s:
        await provider_keys.save(s, "jina", "new_portal_key_9999", "boss@x.com")
    assert provider_keys.current("jina") == "new_portal_key_9999"
    _new_master(monkeypatch)                       # this process now has the wrong master key
    async with db() as s:
        await provider_keys.refresh(s, force=True)
    assert provider_keys.current("jina") == ""     # not the leaked environment key
    assert provider_keys.source("jina") == "unreadable"
    assert provider_keys.describe()["jina"]["decrypt_failed"] is True
    provider_keys.reset_for_tests()


async def test_key_changes_are_audited_and_processes_report_what_they_use(db, monkeypatch, api_key):
    import httpx

    from app import provider_keys, providers
    from app.main import app

    provider_keys.reset_for_tests()
    _new_master(monkeypatch)

    async def ok_status(client, key):
        return "ok"

    monkeypatch.setattr(providers, "typesafe_key_status", ok_status)
    provider_keys.set_process_name("api")
    h = {"X-API-Key": api_key, "X-Request-ID": "req-audit-1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.put("/providers/keys/typesafe", json={"key": "ts_new_key_abcd"},
                            headers=h)).status_code == 200
        assert (await c.delete("/providers/keys/typesafe", headers=h)).status_code == 200
        body = (await c.get("/providers/keys", headers=h)).json()
    actions = [(a["provider"], a["action"], a["last4"]) for a in body["audit"]]
    assert actions[:2] == [("typesafe", "removed", None), ("typesafe", "saved", "abcd")]
    assert body["processes"]["api"]["typesafe"]["source"] in ("environment", "none")
    async with db() as s:
        rid = await s.scalar(text("SELECT request_id FROM provider_key_audit WHERE action='saved'"))
    assert rid == "req-audit-1"
    provider_keys.reset_for_tests()


async def test_key_changes_are_rate_limited_even_for_admins(db, monkeypatch, api_key):
    import httpx

    from app import main as main_mod
    from app import providers
    from app.main import app

    _new_master(monkeypatch)
    main_mod._key_changes.clear()

    async def rejected(client, key):
        return "rejected"

    monkeypatch.setattr(providers, "typesafe_key_status", rejected)
    h = {"X-API-Key": api_key}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        codes = [(await c.put("/providers/keys/typesafe", json={"key": f"probe_key_{i:04d}"},
                              headers=h)).status_code for i in range(12)]
    assert codes[:10] == [422] * 10 and codes[10:] == [429, 429]
    main_mod._key_changes.clear()


async def test_a_nearly_empty_jina_key_does_not_resume_jobs(db, monkeypatch, api_key):
    import httpx

    from app import main as main_mod
    from app import provider_keys, providers, queue
    from app.main import app

    provider_keys.reset_for_tests()
    _new_master(monkeypatch)
    main_mod._key_changes.clear()

    async def low(client, key):
        return 5_000_000                                  # under JINA_RESUME_MIN_TOKENS

    monkeypatch.setattr(providers, "jina_balance", low)
    job = await make_job(db, ["low1.com"])
    async with db() as s:
        await queue.pause_active_jobs(s, "jina_account")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.put("/providers/keys/jina", json={"key": "jina_low_key_1111"},
                        headers={"X-API-Key": api_key})
    assert r.status_code == 200 and r.json()["resumed_jobs"] == 0
    async with db() as s:
        assert await job_status(s, job.id) == "paused"
    provider_keys.reset_for_tests()


async def test_estimate_uses_the_active_span_not_the_whole_window(db, api_key):
    import httpx

    from app.main import app

    job = await make_job(db, [f"span{i}.com" for i in range(40)])
    async with db() as s:
        # 40 domains finished within one minute, fourteen minutes ago; idle since.
        await s.execute(text("""
            UPDATE job_domains SET state = 'done', status = 'found', jina_tokens = 10,
                finished_at = now() - interval '14 minutes'
                              + (random() * interval '60 seconds')
            WHERE job_id = :j
        """), {"j": job.id})
        await s.execute(text("UPDATE jobs SET status = 'done'"))
        await s.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        e = (await c.get("/estimate?domains=4000", headers={"X-API-Key": api_key})).json()
    assert e["basis"] == "recent" and e["rate_per_minute"] >= 35       # not 40/15 = 2.7


async def test_worker_retries_with_a_newly_saved_key_instead_of_pausing(db, monkeypatch):
    from app import provider_keys
    from app import worker as worker_mod
    from app.pipeline.fetch import ErrorClass, FetchError
    from app.schemas import DomainResult

    provider_keys.reset_for_tests()
    _new_master(monkeypatch)
    calls = []

    async def first_402_then_ok(domain, fetcher=None):
        calls.append(domain)
        if len(calls) == 1:
            # An admin saves a new key while this request is failing with the old one.
            async with db() as s:
                await provider_keys.save(s, "jina", "fresh_key_after_topup_1", "boss@x.com")
            provider_keys.reset_for_tests()            # this process has not seen it yet
            raise FetchError(ErrorClass.PROVIDER_ACCOUNT, "provider_account", detail="402")
        return DomainResult(domain=domain, status="found", best_email=f"a@{domain}")

    monkeypatch.setattr(worker_mod, "process_domain", first_402_then_ok)
    job = await make_job(db, ["retry-key.com"])

    async def done(s):
        return await job_status(s, job.id) == "done"

    await run_until(db, done)
    assert len(calls) == 2
    async with db() as s:
        assert await s.scalar(text("SELECT count(*) FROM jobs WHERE status = 'paused'")) == 0
    provider_keys.reset_for_tests()


# --- fourth review: races the queue lost ---------------------------------------


async def test_two_claims_finishing_a_jobs_last_domains_together_complete_it(db):
    """Both finishers used to see the other's row as still running and neither marked
    the job done, so it stayed 'running' forever."""
    from app import queue
    from app.schemas import DomainResult

    job = await make_job(db, ["a.com", "b.com"])
    async with db() as s:
        c1 = await queue.claim(s, "w")
        c2 = await queue.claim(s, "w")
    assert {c1.domain, c2.domain} == {"a.com", "b.com"}

    async def finish(c):
        async with db() as s:
            return await queue.finish(s, c, DomainResult(domain=c.domain, status="no_contact_info"),
                                      from_cache=False)

    # Each finish opens its own session and transaction; run them at once, many times.
    results = await asyncio.gather(finish(c1), finish(c2))
    assert sum(1 for recorded, done in results if done) == 1
    async with db() as s:
        assert await job_status(s, job.id) == "done"


async def test_a_lost_claim_releases_its_domain_lock(db):
    """The handler whose row was reaped kept the domain lock, and the per-worker
    heartbeat renewed it forever: every job holding that domain stalled."""
    from app import queue
    from app.schemas import DomainResult

    await make_job(db, ["stuck.com"])
    async with db() as s:
        old = await queue.claim(s, "w")
        assert await queue.lock_domain(s, old)
        await s.execute(text("UPDATE job_domains SET heartbeat_at = now() - interval '1 hour'"))
        await s.commit()
        await queue.reap(s)
        new = await queue.claim(s, "w")
        r = DomainResult(domain="stuck.com", status="found", best_email="a@stuck.com")
        assert await queue.finish(s, old, r, from_cache=False) == (False, False)
        assert await s.scalar(text("SELECT lock_owner FROM domains")) is None
        assert await queue.lock_domain(s, new)


async def test_heartbeat_renews_only_live_claims(db):
    from app import queue

    await make_job(db, ["live.com"])
    await make_job(db, ["dead.com"])
    async with db() as s:
        live = await queue.claim(s, "w")
        dead = await queue.claim(s, "w")
        assert await queue.lock_domain(s, live) and await queue.lock_domain(s, dead)
        await s.execute(text("UPDATE job_domains SET heartbeat_at = now() - interval '1 hour'"))
        await s.execute(text("UPDATE domains SET lock_until = now() - interval '1 hour'"))
        await s.commit()
        await queue.heartbeat(s, [live.token])
        assert await queue.reap(s) == 1
        state = dict((await s.execute(text(
            "SELECT domain, state FROM job_domains"))).all())
        assert state == {"live.com": "running", "dead.com": "queued"}
        expired = await s.scalar(text(
            "SELECT count(*) FROM domains WHERE lock_until < now()"))
        assert expired == 1


async def test_the_sweep_completes_a_job_that_slipped_through(db):
    from app import queue

    job = await make_job(db, ["x.com"])
    async with db() as s:
        await s.execute(text("UPDATE job_domains SET state = 'done'"))
        await s.execute(text("UPDATE jobs SET status = 'running'"))
        await s.commit()
        assert await queue.complete_finished_jobs(s) == 1
        assert await queue.complete_finished_jobs(s) == 0
        assert await job_status(s, job.id) == "done"


# --- retry failed domains, worker health ------------------------------------------


async def _finish_all(db, job_id, *, status, reason=None, site_status=None):
    from app import queue
    from app.schemas import DomainResult

    while True:
        async with db() as s:
            c = await queue.claim(s, "w")
            if c is None or c.job_id != job_id:
                return
            await queue.finish(s, c, DomainResult(domain=c.domain, status=status,
                                                  error_reason=reason, site_status=site_status),
                               from_cache=False)


async def test_retry_requeues_transient_failures_and_bypasses_their_cached_result(db, api_key):
    import httpx

    from app import queue
    from app.main import app

    job = await make_job(db, ["slow.com", "gone.com", "fine.com"])
    async with db() as s:
        # slow.com timed out, gone.com was a real answer, fine.com was found.
        for domain, status, reason in (("slow.com", "fetch_failed", "domain_timeout"),
                                       ("gone.com", "no_contact_info", None),
                                       ("fine.com", "found", None)):
            c = await queue.claim(s, "w")
            while c.domain != domain:
                await queue.requeue(s, c, 0, count_attempt=False)
                c = await queue.claim(s, "w")
            from app.schemas import DomainResult
            await queue.finish(s, c, DomainResult(domain=domain, status=status,
                                                  error_reason=reason,
                                                  best_email="a@fine.com" if status == "found" else None),
                               from_cache=False)
        assert await job_status(s, job.id) == "done"

    h = {"X-API-Key": api_key}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(f"/jobs/{job.id}/retry", headers=h)
        assert r.status_code == 200
        assert r.json() == {"job_id": str(job.id), "requeued": 1, "status": "running"}
        # Nothing left to retry: still fine, still running (the row is queued).
        assert (await c.post(f"/jobs/{job.id}/retry", headers=h)).json()["requeued"] == 0

    async with db() as s:
        rows = dict((await s.execute(text(
            "SELECT domain, state FROM job_domains WHERE job_id = :j"), {"j": job.id})).all())
        assert rows == {"slow.com": "queued", "gone.com": "done", "fine.com": "done"}
        # The failure it is retrying is in the shared cache, finished after the job
        # was created; the requeued row must not read it back.
        claim = await queue.claim(s, "w")
        assert claim.domain == "slow.com" and claim.attempts == 1 and claim.not_before
        job_row = await s.get(__import__("app.models", fromlist=["Job"]).Job, job.id)
        assert await queue.cached_result(s, job_row, "slow.com") is not None      # without the floor
        assert await queue.cached_result(s, job_row, "slow.com", claim.not_before) is None


async def test_retry_refuses_paused_and_cancelled_jobs(db, api_key):
    import httpx

    from app.main import app

    job = await make_job(db, ["x.com"])
    h = {"X-API-Key": api_key}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        async with db() as s:
            await s.execute(text("UPDATE jobs SET status = 'paused', pause_reason = 'jina_account'"))
            await s.commit()
        r = await c.post(f"/jobs/{job.id}/retry", headers=h)
        assert r.status_code == 409 and r.json()["error"]["code"] == "job_not_retryable"
        async with db() as s:
            await s.execute(text("UPDATE jobs SET status = 'cancelled'"))
            await s.commit()
        assert (await c.post(f"/jobs/{job.id}/retry", headers=h)).status_code == 409


async def test_retry_books_the_time_a_job_sat_finished_as_paused(db):
    from app import queue

    job = await make_job(db, ["t.com"])
    await _finish_all(db, job.id, status="fetch_failed", reason="homepage")
    async with db() as s:
        await s.execute(text(
            "UPDATE jobs SET finished_at = now() - interval '1 hour', "
            "started_at = now() - interval '2 hours'"))
        await s.commit()
        assert await queue.requeue_failed(s, job.id) == 1
        row = (await s.execute(text(
            "SELECT status::text, finished_at, paused_seconds FROM jobs WHERE id = :j"),
            {"j": job.id})).one()
    assert row[0] == "running" and row[1] is None and 3590 < row[2] < 3610


async def test_worker_health_is_written_on_heartbeat_and_read_by_status(db, monkeypatch):
    from app import provider_keys
    from app.providers import worker_health
    from app.worker import Worker

    provider_keys.set_process_name("worker")
    w = Worker(concurrency=7, fetcher=object())
    w._live.add("w|abc")
    async with db() as s:
        await provider_keys.refresh(s, force=True)          # the key report
        await provider_keys.report_health(s, w.health())    # must not overwrite it
        row = await s.scalar(text("SELECT status FROM process_status WHERE name = 'worker'"))
        assert row["health"]["in_flight"] == 1 and row["health"]["concurrency"] == 7
        assert "jina" in row                                # the key report survived
        health = await worker_health(s)
        assert health["status"] == "ok" and health["in_flight"] == 1
        await s.execute(text("UPDATE process_status SET updated_at = now() - interval '10 minutes'"))
        await s.commit()
        assert (await worker_health(s))["status"] == "silent"
    async with db() as s:
        await s.execute(text("DELETE FROM process_status"))
        await s.commit()
        assert (await worker_health(s))["status"] == "never_seen"
