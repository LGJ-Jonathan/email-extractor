"""FastAPI app (spec sections 5 and 14)."""

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import jobs as jobs_svc
from app import queue
from app.auth import Principal, require_api_key
from app.db import dispose_engine, get_session
from app.ingest import IngestError, detect_column, parse_bytes, preview
from app.models import Job
from app.pipeline.fetch import build_fetcher
from app.pipeline.run import process_input
from app.schemas import MAX_ITEMS_PER_JOB, CreateJobRequest, DomainResult, ExtractRequest
from app.settings import settings
from app.upload_page import UPLOAD_PAGE
from app.webhook import valid_webhook_url

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
    if settings.api_key == "change-me":
        log.warning("API_KEY is still the default value; set it before exposing this service")
    yield
    from app.pipeline.fetch import close_client

    await close_client()
    await dispose_engine()


app = FastAPI(title="Website Email Extractor", version="0.1.0", lifespan=lifespan)


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
    return await process_input(req.url, _get_fetcher())


# --- jobs -----------------------------------------------------------------


async def _read_upload(file: UploadFile) -> tuple[bytes, str]:
    raw = await file.read()
    return raw, file.filename or "upload.csv"


@app.post("/jobs/preview", dependencies=[Depends(require_api_key)])
async def jobs_preview(file: UploadFile = File(...)) -> dict:
    """Spec 5: detect the website column and describe the file. Creates no job."""
    raw, filename = await _read_upload(file)
    try:
        parsed = parse_bytes(raw, filename)
    except IngestError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
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
            raise HTTPException(status_code=422, detail=str(e)) from e
        guess = detect_column(parsed)
        chosen = column or guess.column or guess.email_column
        if not chosen:
            raise HTTPException(
                status_code=422,
                detail="no website column detected; pass column=<name>",
            )
        items = jobs_svc.prepare_items(parsed, guess, chosen)
        source_columns, source_name = parsed.headers, filename
    else:
        body = CreateJobRequest.model_validate(await request.json())
        if len(body.items) > MAX_ITEMS_PER_JOB:
            raise HTTPException(status_code=413, detail=f"max {MAX_ITEMS_PER_JOB:,} items")
        items = jobs_svc.prepare_plain(body.items)
        source_columns, source_name = ["website"], None
        webhook_url, fresh, chosen = body.webhook_url, body.fresh, "website"

    if len(items) > MAX_ITEMS_PER_JOB:
        raise HTTPException(status_code=413, detail=f"max {MAX_ITEMS_PER_JOB:,} rows")
    if not items:
        raise HTTPException(status_code=422, detail="no rows")
    if not valid_webhook_url(webhook_url):
        raise HTTPException(status_code=422, detail="webhook_url must be http(s)")
    if not who.is_admin:
        active = await session.scalar(
            select(func.count()).select_from(Job).where(
                Job.owner_id == who.user_id, Job.status.in_(ACTIVE_STATUSES)
            )
        )
        if active >= settings.max_active_jobs_per_user:
            raise HTTPException(
                status_code=429,
                detail=f"you already have {active} unfinished jobs "
                       f"(max {settings.max_active_jobs_per_user}); wait or cancel one",
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
        raise HTTPException(status_code=404, detail="unknown job") from e
    job = await session.scalar(select(Job).where(Job.id == parsed_id))
    if job is None or not (who.is_admin or job.owner_id == who.user_id):
        raise HTTPException(status_code=404, detail="unknown job")
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
        async for raw_row, result, pending, retrying in jobs_svc.stream_rows(session, job):
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
        async for raw_row, result, pending, retrying in jobs_svc.stream_rows(session, job):
            yield jobs_svc.json_line(columns, raw_row, result, pending_domain=pending,
                                     retrying=retrying)

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# --- operator endpoints (spec 17) ----------------------------------------


@app.post("/jobs/{job_id}/resume")
async def resume_job(
    job_id: str,
    who: Principal = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
) -> dict:
    job = await _get_job(session, job_id, who)
    if job.status != "paused":
        raise HTTPException(status_code=409, detail=f"job is {job.status}, not paused")
    job.status = "running"
    job.pause_reason = None
    await session.commit()
    # Everything may have finished before the pause landed.
    await queue.complete_if_finished(session, job.id)
    await session.refresh(job)
    return {"job_id": str(job.id), "status": job.status}


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    who: Principal = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Stop claiming this job's domains. Domains already in flight finish normally."""
    job = await _get_job(session, job_id, who)
    if job.status not in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail=f"job is already {job.status}")
    job.status = "failed"
    job.pause_reason = "cancelled"
    await session.commit()
    return {"job_id": str(job.id), "status": job.status}


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
