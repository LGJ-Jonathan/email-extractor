"""FastAPI app (spec sections 5 and 14)."""

import json
import logging
import uuid
from contextlib import asynccontextmanager

import pydantic
from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import jobs as jobs_svc
from app import queue
from app import errors
from app.auth import Principal, admin_key_usable, require_api_key
from app.errors import ApiError
from app.db import dispose_engine, get_session, get_sessionmaker
from app.ingest import IngestError, detect_column, parse_bytes, preview
from app.models import Job
from app.pipeline.fetch import ErrorClass, FetchError, build_fetcher
from app.pipeline.run import process_input
from app.schemas import MAX_ITEMS_PER_JOB, CreateJobRequest, DomainResult, ExtractRequest
from app.settings import settings
from app.upload_page import UPLOAD_PAGE
from app.webhook import WebhookRejected, validate_webhook_url

logging.basicConfig(level=settings.log_level)
log = logging.getLogger("email_extractor")


ACTIVE_STATUSES = ("queued", "running", "paused")

# One fetcher for the process, so /extract calls share one gate (bucket, breaker,
# concurrency) instead of each building an unlimited one.
_fetcher = None


def _get_fetcher():
    global _fetcher
    if _fetcher is None:
        _fetcher = build_fetcher()
    return _fetcher


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not admin_key_usable():
        log.error("API_KEY is a placeholder or shorter than 32 characters; the admin key "
                  "is disabled until it is replaced")
    yield
    from app.pipeline.fetch import close_client

    await close_client()
    await dispose_engine()


app = FastAPI(title="Website Email Extractor", version="0.1.0", lifespan=lifespan)
errors.install(app)

# Headroom over the file limit for multipart framing and the other form fields.
BODY_LIMIT = settings.max_upload_bytes + 1024 * 1024


class _BodyTooLarge(Exception):
    pass


class BodySizeLimit:
    """Refuse oversized request bodies while they stream in, not after.

    Without this, a 2 GB upload or a 5M-item JSON array was read in full (to disk for
    multipart, to memory for JSON) before any size check ran.
    """

    def __init__(self, app, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.limit:
            return await self._reject(send, scope)
        seen = 0
        started = False

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.limit:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await self._reject(send, scope)

    async def _reject(self, send, scope=None) -> None:
        mb = settings.max_upload_bytes // (1024 * 1024)
        rid = ((scope or {}).get("state") or {}).get("request_id")
        body = json.dumps(errors.error_body(
            "file_too_large", f"request body is over the {mb} MB limit", rid)).encode()
        headers = [(b"content-type", b"application/json"),
                   (b"content-length", str(len(body)).encode())]
        if rid:
            headers.append((b"x-request-id", rid.encode()))
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(BodySizeLimit, limit=BODY_LIMIT)
app.add_middleware(errors.RequestId)            # outermost: every response gets an id


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Unauthenticated liveness probe. Exposes nothing but a constant."""
    return {"status": "ok"}


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
async def upload_page() -> str:
    """Spec 14: one static page, plain HTML + fetch, no framework.

    Unauthenticated because it contains no data -- the API key is typed into it by the
    operator and held in sessionStorage for that tab only.
    """
    return UPLOAD_PAGE


@app.post("/extract", response_model=DomainResult, dependencies=[Depends(require_api_key)])
async def extract(req: ExtractRequest) -> DomainResult:
    try:
        return await process_input(req.url, _get_fetcher())
    except FetchError as e:
        if e.error_class is not ErrorClass.PROVIDER_ACCOUNT:
            raise
        # Was a bare 500. It is the provider account (quota, key), not this request.
        raise ApiError(
            "provider_quota_exhausted",
            f"the {settings.fetch_backend} account is out of credit or rejecting our key; "
            "lookups resume once it is topped up",
        ) from e


# --- jobs -----------------------------------------------------------------


async def _read_upload(file: UploadFile) -> tuple[bytes, str]:
    """Read at most max_upload_bytes + 1, so an oversized file is refused, not buffered."""
    cap = settings.max_upload_bytes
    raw = await file.read(cap + 1)
    if len(raw) > cap:
        raise ApiError("file_too_large", f"file is over the {cap // (1024 * 1024)} MB limit",
                       details={"limit_bytes": cap})
    return raw, file.filename or "upload.csv"


@app.post("/jobs/preview", dependencies=[Depends(require_api_key)])
async def jobs_preview(file: UploadFile = File(...)) -> dict:
    """Spec 5: detect the website column and describe the file. Creates no job."""
    raw, filename = await _read_upload(file)
    try:
        parsed = parse_bytes(raw, filename)
    except IngestError as e:
        raise ApiError(e.code, str(e)) from e
    guess = detect_column(parsed)
    return {"filename": filename, **preview(parsed, guess)}


@app.post("/jobs")
async def create_job(
    request: Request,
    who: Principal = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
    file: UploadFile | None = File(default=None),
    column: str | None = Form(default=None),
    webhook_url: str | None = Form(default=None),
    fresh: bool = Form(default=False),
) -> dict:
    """Spec 5: JSON body of items, or a multipart file upload."""
    if file is not None:
        raw, filename = await _read_upload(file)
        try:
            parsed = parse_bytes(raw, filename)
        except IngestError as e:
            raise ApiError(e.code, str(e)) from e
        guess = detect_column(parsed)
        if column and column not in parsed.headers:
            raise ApiError("unknown_column", f"the file has no column named {column!r}",
                           details={"columns": parsed.headers})
        chosen = column or guess.column or guess.email_column
        if not chosen:
            raise ApiError("no_website_column",
                           "no website column was detected; choose one and pass column=",
                           details={"columns": parsed.headers})
        items = jobs_svc.prepare_items(parsed, guess, chosen)
        source_columns, source_name = parsed.headers, filename
    else:
        # Both of these were bare 500s: malformed JSON, and {"items": "acme.com"}.
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ApiError("invalid_json", "the request body is not valid JSON") from e
        try:
            body = CreateJobRequest.model_validate(payload)
        except pydantic.ValidationError as e:
            details = [{"field": ".".join(str(p) for p in err["loc"]), "msg": err["msg"]}
                       for err in e.errors()]
            first = details[0]
            raise ApiError("validation_error", f"{first['field']}: {first['msg']}",
                           details=details) from e
        if len(body.items) > MAX_ITEMS_PER_JOB:
            raise ApiError("too_many_rows",
                           f"{len(body.items):,} items; the limit is {MAX_ITEMS_PER_JOB:,}",
                           details={"limit": MAX_ITEMS_PER_JOB})
        items = jobs_svc.prepare_plain(body.items)
        source_columns, source_name = ["website"], None
        webhook_url, fresh, chosen = body.webhook_url, body.fresh, "website"

    if len(items) > MAX_ITEMS_PER_JOB:
        raise ApiError("too_many_rows",
                       f"{len(items):,} rows; the limit is {MAX_ITEMS_PER_JOB:,}",
                       details={"limit": MAX_ITEMS_PER_JOB})
    if not items:
        raise ApiError("no_rows", "there are no rows to process")
    try:
        await validate_webhook_url(webhook_url)
    except WebhookRejected as e:
        raise ApiError("invalid_webhook_url", str(e)) from e
    if not who.is_admin:
        # Serialise this user's job creation, so two simultaneous POSTs cannot both
        # pass the count. Released when create_job commits.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"jobs:{who.user_id}"}
        )
        active = await session.scalar(
            select(func.count()).select_from(Job).where(
                Job.owner_id == who.user_id, Job.status.in_(ACTIVE_STATUSES)
            )
        )
        if active >= settings.max_active_jobs_per_user:
            raise ApiError(
                "job_limit_reached",
                f"you already have {active} unfinished jobs (the limit is "
                f"{settings.max_active_jobs_per_user}); wait for one to finish or cancel it",
                details={"active": active, "limit": settings.max_active_jobs_per_user},
            )

    job, to_run, cached = await jobs_svc.create_job(
        session, items,
        source_columns=source_columns,
        filename=source_name,
        website_column=chosen,
        webhook_url=webhook_url,
        fresh=fresh,
        owner_id=who.user_id,
    )
    return {
        "job_id": str(job.id),
        "total": len(items),
        "unique_domains": len(to_run) + len(cached),
        "cached": len(cached),
        "queued": len(to_run),
    }


async def _get_job(session: AsyncSession, job_id: str, who: Principal) -> Job:
    """404, not 403, for someone else's job: do not confirm that it exists."""
    try:
        parsed_id = uuid.UUID(job_id)
    except ValueError as e:
        raise ApiError("job_not_found", "no such job") from e
    job = await session.scalar(select(Job).where(Job.id == parsed_id))
    if job is None or not (who.is_admin or job.owner_id == who.user_id):
        raise ApiError("job_not_found", "no such job")
    return job


@app.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    who: Principal = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
) -> dict:
    job = await _get_job(session, job_id, who)
    counts = await jobs_svc.job_counts(session, job.id)
    return {
        "job_id": str(job.id),
        "status": job.status,
        "paused": job.status == "paused",
        "pause_reason": job.pause_reason,
        "filename": job.filename,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        **counts,
    }


@app.get("/jobs/{job_id}/results.csv")
async def results_csv(
    job_id: str,
    who: Principal = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
):
    job = await _get_job(session, job_id, who)
    columns = list(job.columns or ["website"])

    async def gen():
        # UTF-8 BOM so Excel opens it correctly (spec 14).
        yield "﻿"
        yield jobs_svc.csv_line(jobs_svc.output_header(columns))
        # Its own session: FastAPI closes the request's yield-dependency session before
        # a streaming body runs, and streaming on it leaked a connection per download.
        async with get_sessionmaker()() as s:
            async for raw_row, result, pending, retrying in jobs_svc.stream_rows(s, job):
                yield jobs_svc.csv_line(
                    jobs_svc.output_row(columns, raw_row, result, pending_domain=pending,
                                        retrying=retrying)
                )

    name = (job.filename or "results").rsplit(".", 1)[0]
    return StreamingResponse(
        gen(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}_extracted.csv"'},
    )


@app.get("/jobs/{job_id}/results.json")
async def results_json(
    job_id: str,
    who: Principal = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
):
    job = await _get_job(session, job_id, who)
    columns = list(job.columns or ["website"])

    async def gen():
        async with get_sessionmaker()() as s:
            async for raw_row, result, pending, retrying in jobs_svc.stream_rows(s, job):
                yield jobs_svc.json_line(columns, raw_row, result, pending_domain=pending,
                                         retrying=retrying)

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# --- operator endpoints (spec 17) ----------------------------------------


async def _transition(session: AsyncSession, job: Job, allowed: tuple[str, ...],
                      new_status: str, reason: str | None) -> str | None:
    """Change status only if it is still one of `allowed`; None when it was not.

    Read-then-write let a cancel landing between resume's read and commit bring a
    cancelled job back to running, and let a cancel overwrite a job just marked done.
    """
    row = (await session.execute(
        text("""
            UPDATE jobs SET status = CAST(:new AS job_status), pause_reason = :reason
            WHERE id = :j AND status::text = ANY(:allowed)
            RETURNING status::text
        """),
        {"new": new_status, "reason": reason, "j": job.id, "allowed": list(allowed)},
    )).first()
    await session.commit()
    return row[0] if row else None


async def _current_status(session: AsyncSession, job: Job) -> str:
    return await session.scalar(
        text("SELECT status::text FROM jobs WHERE id = :j"), {"j": job.id}
    )


@app.post("/jobs/{job_id}/resume")
async def resume_job(
    job_id: str,
    who: Principal = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
) -> dict:
    job = await _get_job(session, job_id, who)
    if await _transition(session, job, ("paused",), "running", None) is None:
        status = await _current_status(session, job)
        raise ApiError("job_not_paused", f"the job is {status}, not paused",
                       details={"status": status})
    # Everything may have finished before the pause landed.
    await queue.complete_if_finished(session, job.id)
    return {"job_id": str(job.id), "status": await _current_status(session, job)}


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    who: Principal = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Stop claiming this job's domains. Domains already in flight finish normally."""
    job = await _get_job(session, job_id, who)
    if await _transition(session, job, ACTIVE_STATUSES, "cancelled", None) is None:
        status = await _current_status(session, job)
        raise ApiError("job_already_finished", f"the job is already {status}",
                       details={"status": status})
    return {"job_id": str(job.id), "status": "cancelled"}


@app.get("/jobs")
async def list_jobs(
    who: Principal = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The caller's 50 most recent jobs (an admin sees everyone's)."""
    stmt = select(Job).order_by(Job.created_at.desc()).limit(50)
    if not who.is_admin:
        stmt = stmt.where(Job.owner_id == who.user_id)
    jobs = list(await session.scalars(stmt))
    return {
        "user": who.name,
        "jobs": [
            {
                "job_id": str(j.id),
                "status": j.status,
                "pause_reason": j.pause_reason,
                "filename": j.filename,
                "total": j.total,
                "created_at": j.created_at.isoformat() if j.created_at else None,
                "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            }
            for j in jobs
        ],
    }
