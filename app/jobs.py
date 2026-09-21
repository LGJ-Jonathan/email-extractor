"""Job creation, cache reuse, status and result streaming (spec sections 5, 9, 14).

The row-shaping half is pure and unit-tested; the persistence half is thin SQLAlchemy so
it can only be exercised against a real Postgres (Thor's compose stack).
"""

import csv
import io
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest import ColumnGuess, ParsedFile, normalize_column
from app.models import Domain, Job, JobItem
from app.pipeline.normalize import normalize_input
from app.schemas import DomainResult
from app.settings import settings

EX_PREFIX = "ex_"

# Spec 8 CSV columns, minus the two that identify the row; these become ex_* columns
# appended after the uploaded file's own columns (spec 14).
RESULT_FIELDS = (
    "domain", "final_domain", "status", "best_email", "best_email_source", "confidence",
    "needs_review", "business_emails", "all_emails", "phones", "contact_form_url",
    "facebook", "instagram", "linkedin", "x", "youtube", "tiktok", "yelp",
    "mx_provider", "site_status", "pages_fetched", "error_reason", "retrying",
)
SOCIAL_FIELDS = ("facebook", "instagram", "linkedin", "x", "youtube", "tiktok", "yelp")


@dataclass
class PreparedItem:
    row_index: int
    input_value: str
    domain: str | None
    status: str | None
    reason: str | None
    domain_source: str
    raw_row: dict[str, str] = field(default_factory=dict)


def prepare_items(
    parsed: ParsedFile, guess: ColumnGuess, column: str | None = None
) -> list[PreparedItem]:
    """Normalise every row, preserving order and the original cells."""
    col = column or guess.column or guess.email_column
    if not col:
        raise ValueError("no website column selected")
    derive = guess.method == "email" and col == guess.email_column
    normalized = normalize_column(parsed, col, derive_from_email=derive)
    out: list[PreparedItem] = []
    for i, (row, n) in enumerate(zip(parsed.rows, normalized)):
        out.append(
            PreparedItem(
                row_index=i,
                input_value=(row.get(col) or "").strip(),
                domain=n.domain,
                status=None if n.ok else "invalid_input",
                reason=n.reason,
                domain_source=n.domain_source,
                raw_row=row,
            )
        )
    return out


def prepare_plain(items: Iterable[str]) -> list[PreparedItem]:
    """The JSON body form of POST /jobs."""
    out: list[PreparedItem] = []
    for i, raw in enumerate(items):
        n = normalize_input(raw)
        out.append(
            PreparedItem(
                row_index=i,
                input_value=(raw or "").strip(),
                domain=n.domain,
                status=None if n.ok else "invalid_input",
                reason=n.reason,
                domain_source=n.domain_source,
                raw_row={"website": (raw or "").strip()},
            )
        )
    return out


def unique_domains(items: list[PreparedItem]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it.domain and it.domain not in seen:
            seen.add(it.domain)
            out.append(it.domain)          # input order, so the top of the file finishes first
    return out


def cache_cutoff() -> datetime:
    return datetime.now(UTC) - timedelta(days=settings.cache_days)


# --- row shaping (pure, unit-tested) --------------------------------------


def result_cells(result: DomainResult | None, *, pending_domain: str | None = None,
                 retrying: bool = False) -> dict[str, str]:
    """The ex_* half of an output row."""
    if result is None:
        return {
            "domain": pending_domain or "",
            "status": "pending",
            "retrying": "true" if retrying else "false",
        }
    cells = {
        "domain": result.domain,
        "final_domain": result.final_domain or "",
        "status": result.status,
        "best_email": result.best_email or "",
        "best_email_source": result.best_email_source or "",
        "confidence": "" if result.confidence is None else f"{result.confidence:.3f}",
        "needs_review": "true" if result.needs_review else "false",
        "business_emails": ", ".join(result.business_emails),
        "all_emails": ", ".join(c.email for c in result.all_emails),
        "phones": ", ".join(result.phones),
        "contact_form_url": result.contact_form_url or "",
        "mx_provider": result.mx_provider or "",
        "site_status": result.site_status or "",
        "pages_fetched": ", ".join(result.pages_fetched),
        "error_reason": result.error_reason or "",
        "retrying": "true" if retrying else "false",
    }
    for network in SOCIAL_FIELDS:
        cells[network] = result.socials.get(network, "")
    return cells


_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: str) -> str:
    return "'" + value if value.startswith(_FORMULA) else value


def output_header(source_columns: list[str]) -> list[str]:
    """Spec 14: the file's own columns first, then the ex_* result columns."""
    return list(source_columns) + [EX_PREFIX + f for f in RESULT_FIELDS]


def output_row(
    source_columns: list[str],
    raw_row: dict[str, str],
    result: DomainResult | None,
    *,
    pending_domain: str | None = None,
    retrying: bool = False,
) -> list[str]:
    cells = result_cells(result, pending_domain=pending_domain, retrying=retrying)
    row = [csv_safe(str(raw_row.get(c, "") or "")) for c in source_columns]
    row += [csv_safe(str(cells.get(f, "") or "")) for f in RESULT_FIELDS]
    return row


def csv_line(values: list[str]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(values)
    return buf.getvalue()


def json_line(source_columns: list[str], raw_row: dict[str, str],
              result: DomainResult | None, **kw) -> str:
    cells = result_cells(result, **kw)
    payload = {c: raw_row.get(c, "") for c in source_columns}
    payload.update({EX_PREFIX + k: v for k, v in cells.items()})
    return json.dumps(payload, ensure_ascii=False) + "\n"


# --- persistence (needs a real Postgres) ----------------------------------


async def create_job(
    session: AsyncSession,
    items: list[PreparedItem],
    *,
    source_columns: list[str],
    filename: str | None = None,
    website_column: str | None = None,
    webhook_url: str | None = None,
    fresh: bool = False,
) -> tuple[Job, list[str], list[str]]:
    """Persist the job and its rows. Returns (job, domains_to_run, cached_domains)."""
    job = Job(
        id=uuid.uuid4(),
        status="queued",
        total=len(items),
        webhook_url=webhook_url,
        filename=filename,
        columns=source_columns,
        website_column=website_column,
    )
    session.add(job)
    await session.flush()

    session.add_all(
        JobItem(
            job_id=job.id,
            row_index=it.row_index,
            input_value=it.input_value,
            domain=it.domain,
            status=it.status,
            reason=it.reason,
            domain_source=it.domain_source,
            raw_row=it.raw_row,
        )
        for it in items
    )

    domains = unique_domains(items)
    cached: list[str] = []
    if domains and not fresh:
        cutoff = cache_cutoff()
        rows = await session.execute(
            select(Domain.domain).where(
                Domain.domain.in_(domains),
                Domain.finished_at.is_not(None),
                Domain.finished_at >= cutoff,
                Domain.status != "fetch_failed",
            )
        )
        cached = [r[0] for r in rows]

    to_run = [d for d in domains if d not in set(cached)]
    if to_run:
        # Idempotent: a domain already known stays put, a new one starts pending.
        await session.execute(
            pg_insert(Domain)
            .values([{"domain": d, "stage": "pending"} for d in to_run])
            .on_conflict_do_nothing(index_elements=[Domain.domain])
        )
    await session.commit()
    return job, to_run, cached


async def job_counts(session: AsyncSession, job_id: uuid.UUID) -> dict:
    """Aggregate from Postgres, not Redis: a Redis restart must not zero the endpoint."""
    total = await session.scalar(
        select(func.count()).select_from(JobItem).where(JobItem.job_id == job_id)
    )
    unique = await session.scalar(
        select(func.count(func.distinct(JobItem.domain))).where(
            JobItem.job_id == job_id, JobItem.domain.is_not(None)
        )
    )
    finished = await session.scalar(
        select(func.count(func.distinct(Domain.domain)))
        .select_from(JobItem)
        .join(Domain, Domain.domain == JobItem.domain)
        .where(JobItem.job_id == job_id, Domain.finished_at.is_not(None))
    )
    found = await session.scalar(
        select(func.count(func.distinct(Domain.domain)))
        .select_from(JobItem)
        .join(Domain, Domain.domain == JobItem.domain)
        .where(
            JobItem.job_id == job_id,
            Domain.finished_at.is_not(None),
            Domain.status == "found",
        )
    )
    failed = await session.scalar(
        select(func.count(func.distinct(Domain.domain)))
        .select_from(JobItem)
        .join(Domain, Domain.domain == JobItem.domain)
        .where(
            JobItem.job_id == job_id,
            Domain.finished_at.is_not(None),
            Domain.status == "fetch_failed",
        )
    )
    return {
        "total": total or 0,
        "unique_domains": unique or 0,
        "done": finished or 0,
        "failed": failed or 0,
        "found": found or 0,
        "hit_rate_so_far": (found or 0) / finished if finished else 0.0,
    }


async def mark_job_done_if_complete(session: AsyncSession, job_id: uuid.UUID) -> bool:
    counts = await job_counts(session, job_id)
    if counts["unique_domains"] and counts["done"] >= counts["unique_domains"]:
        await session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status.in_(("queued", "running")))
            .values(status="done", finished_at=datetime.now(UTC), done_count=counts["done"])
        )
        await session.commit()
        return True
    return False


async def stream_rows(session: AsyncSession, job: Job):
    """Yield (raw_row, DomainResult|None, pending_domain) in input order.

    Uses a server-side cursor: buffering 50k rows plus their jsonb results before the
    first byte defeats the point of streaming.
    """
    stmt = (
        select(JobItem, Domain)
        .outerjoin(Domain, Domain.domain == JobItem.domain)
        .where(JobItem.job_id == job.id)
        .order_by(JobItem.row_index)
        .execution_options(yield_per=500)
    )
    seen_results: dict[str, DomainResult] = {}
    async for item, domain_row in (await session.stream(stmt)):
        result = None
        if domain_row is not None and domain_row.result:
            cached = seen_results.get(item.domain)
            if cached is None:
                try:
                    cached = DomainResult.model_validate(domain_row.result)
                except Exception:  # noqa: BLE001
                    cached = None
                if cached is not None:
                    seen_results[item.domain] = cached
            result = cached
        if result is None and item.status == "invalid_input":
            result = DomainResult(
                domain=item.domain or item.input_value[:255] or "-",
                status="invalid_input",
                error_reason=item.reason,
            )
        yield item.raw_row or {}, result, item.domain
