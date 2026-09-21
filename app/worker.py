"""The fetch worker: `python -m app.worker`.

Pulls domains from the Postgres queue (app/queue.py) and runs the pipeline on up to
GLOBAL_FETCH_CONCURRENCY of them at once. One process is the intended deployment: the
work is network-bound and paced by JINA_RPM, not by CPU. A second process is safe (the
queue, the domain locks and the Redis rate limit are all shared) but gains nothing
unless JINA_RPM is raised.
"""

import asyncio
import logging
import os
import signal
import socket
import uuid

from app import queue
from app.db import dispose_engine, get_sessionmaker
from app.models import Job
from app.pipeline.fetch import ErrorClass, FetchError, build_fetcher, close_client
from app.pipeline.run import RETRYABLE_DOMAIN_REASONS, process_domain
from app.schemas import DomainResult
from app.settings import settings
from app.webhook import WebhookSender

log = logging.getLogger("email_extractor")

IDLE_POLL_S = 1.0
REAP_EVERY_S = 30.0
ERROR_RETRY_DELAY_S = 30.0
# A domain whose processing keeps dying (worker OOM, a crash the pipeline cannot catch)
# is reaped and reclaimed forever unless something stops it.
MAX_CLAIMS = settings.max_domain_attempts + 2


class Worker:
    def __init__(self, concurrency: int | None = None, fetcher=None) -> None:
        self.id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.concurrency = concurrency or settings.global_fetch_concurrency
        self.fetcher = fetcher or build_fetcher()
        self.sessions = get_sessionmaker()
        self.webhooks = WebhookSender()
        self._tasks: set[asyncio.Task] = set()
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def run(self) -> None:
        log.info("worker_start", extra={"worker": self.id, "concurrency": self.concurrency})
        slots = asyncio.Semaphore(self.concurrency)
        await self.webhooks.start()
        background = [asyncio.create_task(self._heartbeat_loop()),
                      asyncio.create_task(self._reap_loop())]
        try:
            while not self._stopping.is_set():
                # Wait for a slot OR the stop signal. A bare acquire() slept through
                # SIGTERM while all slots were busy (up to domain_timeout_s), past
                # Railway's drain window, so the process was killed before it could
                # hand its rows back.
                if not await self._acquire_or_stop(slots):
                    break
                try:
                    async with self.sessions() as s:
                        c = await queue.claim(s, self.id)
                except Exception:  # noqa: BLE001 - database blip: back off, keep going
                    log.exception("claim_failed")
                    c = None
                if c is None:
                    slots.release()
                    try:
                        await asyncio.wait_for(self._stopping.wait(), IDLE_POLL_S)
                    except TimeoutError:
                        pass
                    continue

                async def run_one(c=c):
                    try:
                        await self.handle(c)
                    finally:
                        slots.release()

                self._spawn(run_one())
        finally:
            for t in background:
                t.cancel()
            await self._drain()

    async def _acquire_or_stop(self, slots: asyncio.Semaphore) -> bool:
        acquire = asyncio.ensure_future(slots.acquire())
        stop = asyncio.ensure_future(self._stopping.wait())
        await asyncio.wait({acquire, stop}, return_when=asyncio.FIRST_COMPLETED)
        stop.cancel()
        if self._stopping.is_set():
            if acquire.done() and not acquire.cancelled():
                slots.release()
            else:
                acquire.cancel()
            return False
        return True

    async def _drain(self) -> None:
        """Give in-flight domains back to the queue instead of waiting minutes for them."""
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            async with self.sessions() as s:
                await queue.release_worker(s, self.id)
        except Exception:  # noqa: BLE001 - the reaper recovers them anyway
            log.exception("release_failed")
        await self.webhooks.close()
        log.info("worker_stop", extra={"worker": self.id})

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(queue.HEARTBEAT_S)
            try:
                async with self.sessions() as s:
                    await queue.heartbeat(s, self.id)
            except Exception:  # noqa: BLE001
                log.exception("heartbeat_failed")

    async def _reap_loop(self) -> None:
        while True:
            try:
                async with self.sessions() as s:
                    n = await queue.reap(s)
                if n:
                    log.warning("reaped", extra={"rows": n})
            except Exception:  # noqa: BLE001
                log.exception("reap_failed")
            await asyncio.sleep(REAP_EVERY_S)

    # --- one domain --------------------------------------------------------

    async def handle(self, c: queue.Claim) -> None:
        try:
            await self._handle(c)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let one domain kill the worker
            log.exception("handle_failed", extra={"domain": c.domain, "job_id": str(c.job_id)})
            try:
                async with self.sessions() as s:
                    await queue.unlock_domain(s, c)
                    await queue.requeue(s, c, ERROR_RETRY_DELAY_S)
            except Exception:  # noqa: BLE001 - the reaper will return it
                log.exception("requeue_failed")

    async def _handle(self, c: queue.Claim) -> None:
        async with self.sessions() as s:
            job = await s.get(Job, c.job_id)
            if job is None:
                return
            cached = await queue.cached_result(s, job, c.domain)
            if cached is not None:
                await self._finish(s, job, c, cached, from_cache=True)
                return
            if c.attempts > MAX_CLAIMS:
                # Not an answer about the site, so it stays in this job and never
                # reaches the shared cache that other jobs read.
                result = DomainResult(domain=c.domain, status="fetch_failed",
                                      error_reason="internal_error",
                                      site_status="too_many_attempts")
                await self._finish(s, job, c, result, from_cache=False, write_cache=False)
                return
            if not await queue.lock_domain(s, c):
                await queue.requeue(s, c, queue.LOCK_BUSY_DELAY_S, count_attempt=False)
                return
            # The other job may have finished it between the cache check and the lock.
            cached = await queue.cached_result(s, job, c.domain)
            if cached is not None:
                await queue.unlock_domain(s, c)
                await self._finish(s, job, c, cached, from_cache=True)
                return

        try:
            result = await process_domain(c.domain, self.fetcher)
        except FetchError as e:
            if e.error_class is not ErrorClass.PROVIDER_ACCOUNT:
                raise
            reason = f"{settings.fetch_backend}_account"
            log.error("jobs_paused", extra={"reason": reason, "detail": str(e)})
            async with self.sessions() as s:
                await queue.pause_active_jobs(s, reason)
                await queue.unlock_domain(s, c)
                await queue.requeue(s, c, 0, count_attempt=False)
            return
        finally:
            gate = getattr(self.fetcher, "gate", None)
            if gate is not None:
                gate.release_domain(c.domain)

        async with self.sessions() as s:
            if (result.error_reason in RETRYABLE_DOMAIN_REASONS
                    and c.attempts < settings.max_domain_attempts):
                # Spec 17 level 2: come back after the main pass, when the provider
                # has had time to recover. Downloads show it as pending + retrying.
                await queue.unlock_domain(s, c)
                await queue.requeue(s, c, settings.retry_pass_delay_s)
                return
            job = await s.get(Job, c.job_id)
            await self._finish(s, job, c, result, from_cache=False)

    async def _finish(self, s, job: Job | None, c: queue.Claim, result: DomainResult,
                      *, from_cache: bool, write_cache: bool = True) -> None:
        recorded, job_done = await queue.finish(s, c, result, from_cache=from_cache,
                                                write_cache=write_cache)
        if not recorded:
            # Reaped while we worked and now owned by another claim: that claim
            # records the result and sends the webhook, not this one.
            log.warning("finish_skipped_lost_claim", extra={"domain": c.domain,
                                                            "job_id": str(c.job_id)})
            return
        log.info("domain_done", extra={
            "domain": c.domain, "job_id": str(c.job_id), "status": result.status,
            "from_cache": from_cache, "jina_tokens": 0 if from_cache else result.jina_tokens,
        })
        if job_done:
            log.info("job_done", extra={"job_id": str(c.job_id)})
        if job is None or not job.webhook_url:
            return
        rows = await queue.row_indexes(s, c.job_id, c.domain)
        counts = None
        if job_done:
            from app.jobs import job_counts

            counts = await job_counts(s, c.job_id)
        # Give the connection back before submit(), which waits when the delivery queue
        # is full: holding it there let one slow receiver pin the whole pool, stall
        # heartbeats and get live rows reaped and processed twice.
        await s.commit()
        await s.close()
        await self.webhooks.submit(job.webhook_url, {
            "event": "domain.done",
            "job_id": str(c.job_id),
            "row_indexes": rows,
            "result": result.model_dump(mode="json"),
        })
        if counts is not None:
            await self.webhooks.submit(job.webhook_url, {
                "event": "job.done", "job_id": str(c.job_id), "counts": counts,
            })


async def main() -> None:
    logging.basicConfig(level=settings.log_level)
    worker = Worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop)
    try:
        await worker.run()
    finally:
        await close_client()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
