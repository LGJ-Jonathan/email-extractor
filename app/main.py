"""FastAPI app (spec sections 5 and 14)."""

import logging
import secrets
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import jobs as jobs_svc
from app.db import dispose_engine, get_session
from app.ingest import IngestError, detect_column, parse_bytes, preview
from app.models import Job
from app.pipeline.run import process_input
from app.schemas import MAX_ITEMS_PER_JOB, CreateJobRequest, DomainResult, ExtractRequest
from app.settings import settings
from app.upload_page import UPLOAD_PAGE

logging.basicConfig(level=settings.log_level)
log = logging.getLogger("email_extractor")


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    # compare_digest raises TypeError on non-ASCII str; Starlette decodes headers as
    # latin-1, so any byte >= 0x80 would otherwise surface as an unauthenticated 500.
    if not x_api_key or not secrets.compare_digest(
        x_api_key.encode("utf-8"), settings.api_key.encode("utf-8")
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")


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
    return await process_input(req.url)


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


@app.post("/jobs", dependencies=[Depends(require_api_key)])
async def create_job(
    request: Request,
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

    job, to_run, cached = await jobs_svc.create_job(
        session, items,
        source_columns=source_columns,
        filename=source_name,
        website_column=chosen,
        webhook_url=webhook_url,
        fresh=fresh,
    )
    await _enqueue(job, to_run)
    return {
        "job_id": str(job.id),
        "total": len(items),
        "unique_domains": len(to_run) + len(cached),
        "cached": len(cached),
        "queued": len(to_run),
    }


async def _enqueue(job: Job, domains: list[str]) -> None:
    """Hand the domains to arq. A deterministic job id makes re-enqueue a no-op."""
    try:
        from arq import create_pool
        from arq.connections import RedisSettings

        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        for d in domains:
            await pool.enqueue_job("run_domain", d, str(job.id), _job_id=f"d:{d}")
        await pool.close()
    except Exception as e:  # noqa: BLE001 - the job row exists; a worker can pick it up
        log.error("enqueue_failed", extra={"job_id": str(job.id), "err": type(e).__name__})


async def _get_job(session: AsyncSession, job_id: str) -> Job:
    try:
        parsed_id = uuid.UUID(job_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail="unknown job") from e
    job = await session.scalar(select(Job).where(Job.id == parsed_id))
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return job


@app.get("/jobs/{job_id}", dependencies=[Depends(require_api_key)])
async def get_job(job_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    job = await _get_job(session, job_id)
    counts = await jobs_svc.job_counts(session, job.id)
    return {
        "job_id": str(job.id),
        "status": job.status,
        "paused": job.status == "paused",
        "pause_reason": job.pause_reason,
        "filename": job.filename,
        **counts,
    }


@app.get("/jobs/{job_id}/results.csv", dependencies=[Depends(require_api_key)])
async def results_csv(job_id: str, session: AsyncSession = Depends(get_session)):
    job = await _get_job(session, job_id)
    columns = list(job.columns or ["website"])

    async def gen():
        # UTF-8 BOM so Excel opens it correctly (spec 14).
        yield "﻿"
        yield jobs_svc.csv_line(jobs_svc.output_header(columns))
        async for raw_row, result, pending in jobs_svc.stream_rows(session, job):
            yield jobs_svc.csv_line(
                jobs_svc.output_row(columns, raw_row, result, pending_domain=pending)
            )

    name = (job.filename or "results").rsplit(".", 1)[0]
    return StreamingResponse(
        gen(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}_extracted.csv"'},
    )


@app.get("/jobs/{job_id}/results.json", dependencies=[Depends(require_api_key)])
async def results_json(job_id: str, session: AsyncSession = Depends(get_session)):
    job = await _get_job(session, job_id)
    columns = list(job.columns or ["website"])

    async def gen():
        async for raw_row, result, pending in jobs_svc.stream_rows(session, job):
            yield jobs_svc.json_line(columns, raw_row, result, pending_domain=pending)

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# --- operator endpoints (spec 17) ----------------------------------------


@app.post("/jobs/{job_id}/resume", dependencies=[Depends(require_api_key)])
async def resume_job(job_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    job = await _get_job(session, job_id)
    if job.status != "paused":
        raise HTTPException(status_code=409, detail=f"job is {job.status}, not paused")
    job.status = "running"
    job.pause_reason = None
    await session.commit()
    return {"job_id": str(job.id), "status": job.status}
