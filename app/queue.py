"""The work queue, in Postgres (replaces the spec's arq queues).

Why not arq: arq is one FIFO list, so a 50k-row job queued first holds every later job
until it drains, and one arq job id per domain makes the same domain in a second job a
silent no-op. With several people submitting at once the queue has to choose *which
job* to serve next, and that needs the job rows, which already live here.

Model:
- `job_domains` holds one row per unique domain per job: queued -> running -> done.
- `claim` picks the job whose owner has the fewest domains in flight, then the job
  with the fewest, then the oldest, and takes that job's next domain with
  FOR UPDATE SKIP LOCKED. A 200-row upload therefore starts at once beside a 50k one.
- `domains.lock_owner/lock_until` (held by one claim) stops two jobs fetching the same
  domain together;
  the loser requeues briefly and then reads the winner's result from the cache.
- Workers heartbeat their running rows; `reap` returns rows whose worker went quiet.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job
from app.schemas import DomainResult
from app.settings import settings

LEASE_S = 90            # a worker silent this long has its rows and domain locks taken back
HEARTBEAT_S = 20
LOCK_BUSY_DELAY_S = 10  # another job is fetching this domain right now


@dataclass(frozen=True)
class Claim:
    job_id: uuid.UUID
    domain: str
    attempts: int       # including this one
    token: str          # this claim alone; see new_token


def new_token(worker: str) -> str:
    """Identifies one claim, not the worker.

    A worker runs dozens of domains at once, and after a reap it can reclaim a row it
    was still processing. Guarding writes with the worker id let that stale handler
    finish the row a second time (double webhook, double tokens). Every write that
    touches a claimed row or a domain lock is guarded by this token instead.
    """
    return f"{worker}|{uuid.uuid4().hex[:12]}"


_PICK_JOBS = text("""
WITH running AS (
    SELECT jd.job_id, coalesce(j.owner_id::text, 'admin') AS owner, count(*) AS n
    FROM job_domains jd JOIN jobs j ON j.id = jd.job_id
    WHERE jd.state = 'running'
    GROUP BY jd.job_id, j.owner_id
), owners AS (
    SELECT owner, sum(n) AS n FROM running GROUP BY owner
)
SELECT j.id
FROM jobs j
LEFT JOIN running r ON r.job_id = j.id
LEFT JOIN owners o ON o.owner = coalesce(j.owner_id::text, 'admin')
WHERE j.status IN ('queued', 'running')
  AND EXISTS (
      SELECT 1 FROM job_domains q
      WHERE q.job_id = j.id AND q.state = 'queued' AND q.available_at <= now()
  )
ORDER BY coalesce(o.n, 0), coalesce(r.n, 0), j.created_at
LIMIT 5
""")

_CLAIM_FROM_JOB = text("""
UPDATE job_domains jd
SET state = 'running', claimed_by = :worker, heartbeat_at = now(), attempts = jd.attempts + 1
FROM (
    SELECT job_id, domain FROM job_domains
    WHERE job_id = :job_id AND state = 'queued' AND available_at <= now()
    ORDER BY seq
    LIMIT 1
    FOR UPDATE SKIP LOCKED
) pick
WHERE jd.job_id = pick.job_id AND jd.domain = pick.domain
RETURNING jd.domain, jd.attempts
""")


async def claim(session: AsyncSession, worker: str) -> Claim | None:
    job_ids = [r[0] for r in await session.execute(_PICK_JOBS)]
    for job_id in job_ids:
        token = new_token(worker)
        row = (await session.execute(_CLAIM_FROM_JOB, {"worker": token, "job_id": job_id})).first()
        if row is None:
            continue                      # another worker took the last one
        await session.execute(
            text("UPDATE jobs SET status = 'running' WHERE id = :j AND status = 'queued'"),
            {"j": job_id},
        )
        await session.commit()
        return Claim(job_id=job_id, domain=row[0], attempts=row[1], token=token)
    await session.rollback()
    return None


def cache_usable(job: Job, finished_at: datetime | None, status: str | None,
                 result: dict | None) -> bool:
    """A finished result this job may reuse instead of fetching.

    Anything finished after the job was created is as fresh as a fetch would be, even a
    fetch_failed (it failed moments ago; failing again costs tokens for nothing). Older
    results obey the cache rules: CACHE_DAYS, never fetch_failed, never for fresh jobs.
    """
    if finished_at is None or not result:
        return False
    if finished_at >= job.created_at:
        return True
    if job.fresh or status == "fetch_failed":
        return False
    return finished_at >= datetime.now(UTC) - timedelta(days=settings.cache_days)


async def cached_result(session: AsyncSession, job: Job, domain: str) -> DomainResult | None:
    row = (await session.execute(
        text("SELECT finished_at, status, result FROM domains WHERE domain = :d"), {"d": domain}
    )).first()
    if row is None or not cache_usable(job, row[0], row[1], row[2]):
        return None
    try:
        return DomainResult.model_validate(row[2])
    except Exception:  # noqa: BLE001 - a stale shape from an older version: refetch
        return None


async def lock_domain(session: AsyncSession, c: Claim) -> bool:
    domain, token = c.domain, c.token
    await session.execute(
        text("INSERT INTO domains (domain, stage) VALUES (:d, 'pending') ON CONFLICT DO NOTHING"),
        {"d": domain},
    )
    got = (await session.execute(
        text("""
            UPDATE domains
            SET lock_owner = :w, lock_until = now() + make_interval(secs => :lease),
                stage = 'fetching'
            WHERE domain = :d
              AND (lock_owner IS NULL OR lock_until < now())
            RETURNING domain
        """),
        {"d": domain, "w": token, "lease": LEASE_S},
    )).first()
    await session.commit()
    return got is not None


async def unlock_domain(session: AsyncSession, c: Claim) -> None:
    domain, token = c.domain, c.token
    await session.execute(
        text("""
            UPDATE domains SET lock_owner = NULL, lock_until = NULL,
                stage = CASE WHEN finished_at IS NULL THEN 'pending'::domain_stage ELSE 'done' END
            WHERE domain = :d AND lock_owner = :w
        """),
        {"d": domain, "w": token},
    )
    await session.commit()


async def requeue(session: AsyncSession, c: Claim, delay_s: float,
                  *, count_attempt: bool = True) -> None:
    """Put a claimed row back. count_attempt=False when the domain did no work."""
    await session.execute(
        text("""
            UPDATE job_domains
            SET state = 'queued', claimed_by = NULL, heartbeat_at = NULL,
                available_at = now() + make_interval(secs => :delay),
                attempts = attempts - :refund
            WHERE job_id = :j AND domain = :d AND claimed_by = :w AND state = 'running'
        """),
        {"j": c.job_id, "d": c.domain, "w": c.token, "delay": float(delay_s),
         "refund": 0 if count_attempt else 1},
    )
    await session.commit()


async def finish(session: AsyncSession, c: Claim, result: DomainResult,
                 *, from_cache: bool, write_cache: bool = True) -> tuple[bool, bool]:
    """Record the outcome in one transaction.

    Returns (recorded, job_done). recorded is False when this claim no longer owns the
    row -- it was reaped and someone else has it -- and then nothing is written, so the
    caller must not send a webhook either. write_cache=False keeps a result that is not
    a real answer about the site (too_many_attempts) out of the shared cache.
    """
    payload = result.model_dump_json()
    owned = (await session.execute(
        text("""
            UPDATE job_domains
            SET state = 'done', finished_at = now(), claimed_by = NULL,
                from_cache = :fc, jina_tokens = :tokens,
                status = :status, result = CAST(:result AS jsonb)
            WHERE job_id = :j AND domain = :d AND state = 'running' AND claimed_by = :tok
            RETURNING domain
        """),
        {"j": c.job_id, "d": c.domain, "tok": c.token, "fc": from_cache,
         "tokens": 0 if from_cache else result.jina_tokens,
         "status": result.status, "result": payload},
    )).first()
    if owned is None:
        await session.rollback()
        return False, False
    if not from_cache and write_cache:
        await session.execute(
            text("""
                UPDATE domains
                SET status = :status, result = CAST(:result AS jsonb), stage = 'done',
                    finished_at = now(), updated_at = now(), attempts = attempts + 1
                WHERE domain = :d
            """),
            {"d": c.domain, "status": result.status, "result": payload},
        )
    await session.execute(
        text("""
            UPDATE domains SET lock_owner = NULL, lock_until = NULL,
                stage = CASE WHEN finished_at IS NULL THEN 'pending'::domain_stage ELSE 'done' END
            WHERE domain = :d AND lock_owner = :tok
        """),
        {"d": c.domain, "tok": c.token},
    )
    done = await complete_if_finished(session, c.job_id, commit=False)
    await session.commit()
    return True, done


async def complete_if_finished(session: AsyncSession, job_id: uuid.UUID,
                               *, commit: bool = True) -> bool:
    row = (await session.execute(
        text("""
            UPDATE jobs
            SET status = 'done', finished_at = now(),
                done_count = (SELECT count(*) FROM job_domains WHERE job_id = :j)
            WHERE id = :j AND status IN ('queued', 'running')
              AND NOT EXISTS (
                  SELECT 1 FROM job_domains
                  WHERE job_id = :j AND state IN ('queued', 'running')
              )
            RETURNING id
        """),
        {"j": job_id},
    )).first()
    if commit:
        await session.commit()
    return row is not None


async def pause_active_jobs(session: AsyncSession, reason: str) -> None:
    """Provider account errors (spec 17 level 3). The key is shared, so every job stops."""
    await session.execute(
        text("""
            UPDATE jobs SET status = 'paused', pause_reason = :r, paused_at = now()
            WHERE status IN ('queued', 'running')
        """),
        {"r": reason},
    )
    await session.commit()


async def heartbeat(session: AsyncSession, worker: str) -> None:
    """Keep this worker's claimed rows and domain locks alive."""
    prefix = worker + "|%"
    await session.execute(
        text("UPDATE job_domains SET heartbeat_at = now() "
             "WHERE state = 'running' AND claimed_by LIKE :p"),
        {"p": prefix},
    )
    await session.execute(
        text("""
            UPDATE domains SET lock_until = now() + make_interval(secs => :lease)
            WHERE lock_owner IS NOT NULL AND lock_owner LIKE :p
        """),
        {"p": prefix, "lease": LEASE_S},
    )
    await session.commit()


async def reap(session: AsyncSession) -> int:
    """Return rows held by a worker that stopped heartbeating (crash, OOM, kill -9)."""
    rows = (await session.execute(
        text("""
            UPDATE job_domains
            SET state = 'queued', claimed_by = NULL, heartbeat_at = NULL, available_at = now()
            WHERE state = 'running'
              AND heartbeat_at < now() - make_interval(secs => :lease)
            RETURNING job_id
        """),
        {"lease": LEASE_S},
    )).all()
    await session.commit()
    return len(rows)


async def release_worker(session: AsyncSession, worker: str) -> None:
    """Clean shutdown: hand back everything this worker holds, without using an attempt."""
    await session.execute(
        text("""
            UPDATE job_domains
            SET state = 'queued', claimed_by = NULL, heartbeat_at = NULL,
                available_at = now(), attempts = greatest(attempts - 1, 0)
            WHERE state = 'running' AND claimed_by LIKE :prefix
        """),
        {"prefix": worker + "|%"},
    )
    await session.execute(
        text("""
            UPDATE domains SET lock_owner = NULL, lock_until = NULL,
                stage = CASE WHEN finished_at IS NULL THEN 'pending'::domain_stage ELSE 'done' END
            WHERE lock_owner IS NOT NULL AND lock_owner LIKE :prefix
        """),
        {"prefix": worker + "|%"},
    )
    await session.commit()


async def row_indexes(session: AsyncSession, job_id: uuid.UUID, domain: str) -> list[int]:
    rows = await session.execute(
        text("SELECT row_index FROM job_items WHERE job_id = :j AND domain = :d "
             "ORDER BY row_index"),
        {"j": job_id, "d": domain},
    )
    return [r[0] for r in rows]
