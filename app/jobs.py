"""Job creation, cache reuse, status and result streaming (spec sections 5, 9, 14).

The row-shaping half is pure and unit-tested; the persistence half is thin SQLAlchemy so
it can only be exercised against a real Postgres (Thor's compose stack).
"""

import csv
import io
import json
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Iterable

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest import ColumnGuess, ParsedFile, normalize_column
from app.models import Domain, Job, JobDomain, JobItem
from app.pipeline.normalize import normalize_input
from app.schemas import DomainResult
from app.settings import settings

EX_PREFIX = "ex_"
PARSED_CACHE = 2000

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
    owner_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
) -> tuple[Job, list[str], list[str]]:
    """Persist the job, its rows and its queue entries.

    Returns (job, domains_to_run, cached_domains). Every unique domain is queued, cached
    or not: the worker resolves a cache hit in one query, and doing it there means a
    cache hit gets the same webhook as a fetch.
    """
    job = Job(
        id=uuid.uuid4(),
        status="queued",
        total=len(items),
        webhook_url=webhook_url,
        filename=filename,
        columns=source_columns,
        website_column=website_column,
        fresh=fresh,
        owner_id=owner_id,
        idempotency_key=idempotency_key,
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
        rows = await session.execute(
            select(Domain.domain).where(
                Domain.domain.in_(domains),
                Domain.finished_at.is_not(None),
                Domain.finished_at >= cache_cutoff(),
                Domain.status != "fetch_failed",
            )
        )
        cached = [r[0] for r in rows]

    if domains:
        # Idempotent: a domain already known stays put, a new one starts pending.
        for chunk in _chunks(domains, 5000):
            await session.execute(
                pg_insert(Domain)
                .values([{"domain": d, "stage": "pending"} for d in chunk])
                .on_conflict_do_nothing(index_elements=[Domain.domain])
            )
        seq = 0
        for chunk in _chunks(domains, 5000):
            await session.execute(
                pg_insert(JobDomain).values(
                    [{"job_id": job.id, "domain": d, "seq": seq + i} for i, d in enumerate(chunk)]
                )
            )
            seq += len(chunk)
    else:
        job.status = "done"                  # every row was invalid_input
        job.finished_at = datetime.now(UTC)
    await session.commit()
    cached_set = set(cached)
    return job, [d for d in domains if d not in cached_set], cached


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


_COUNTS = text("""
SELECT
    count(*) AS unique_domains,
    count(*) FILTER (WHERE jd.state = 'done') AS done,
    count(*) FILTER (WHERE jd.state = 'done' AND jd.status = 'found') AS found,
    count(*) FILTER (WHERE jd.state = 'done' AND jd.status = 'fetch_failed') AS failed,
    count(*) FILTER (WHERE jd.state = 'running') AS running,
    count(*) FILTER (WHERE jd.state = 'queued' AND jd.attempts > 0) AS retrying,
    count(*) FILTER (WHERE jd.from_cache) AS cached,
    coalesce(sum(jd.jina_tokens), 0) AS jina_tokens,
    count(*) FILTER (WHERE jd.state = 'done' AND NOT jd.from_cache
                     AND jd.jina_tokens > 0) AS fetched,
    count(*) FILTER (WHERE jd.needs_review) AS needs_review,
    count(*) FILTER (WHERE jd.typesafe_called) AS typesafe_calls,
    count(*) FILTER (WHERE jd.has_phone) AS with_phone,
    count(*) FILTER (WHERE jd.has_form) AS with_form,
    count(*) FILTER (WHERE jd.has_linkedin) AS with_linkedin
FROM job_domains jd
WHERE jd.job_id = :j
""")

_BY_STATUS = text("""
SELECT jd.status, count(*) FROM job_domains jd
WHERE jd.job_id = :j AND jd.state = 'done' AND jd.status IS NOT NULL
GROUP BY jd.status
""")

_ROWS = text("""
SELECT count(*) AS total,
       count(*) FILTER (WHERE status = 'invalid_input') AS invalid,
       count(*) FILTER (WHERE reason = 'empty') AS empty
FROM job_items WHERE job_id = :j
""")


async def job_counts(session: AsyncSession, job_id: uuid.UUID) -> dict:
    """Aggregate from Postgres, not Redis: a Redis restart must not zero the endpoint."""
    rows = (await session.execute(_ROWS, {"j": job_id})).mappings().one()
    r = (await session.execute(_COUNTS, {"j": job_id})).mappings().one()
    by_status = {k: v for k, v in (await session.execute(_BY_STATUS, {"j": job_id})).all()}
    done, found = r["done"] or 0, r["found"] or 0
    fetched = r["fetched"] or 0
    return {
        "total": rows["total"] or 0,
        "invalid_rows": rows["invalid"] or 0,
        "empty_rows": rows["empty"] or 0,
        "by_status": by_status,
        "needs_review": r["needs_review"] or 0,
        "typesafe_calls": r["typesafe_calls"] or 0,
        "tokens_per_domain": round((r["jina_tokens"] or 0) / fetched) if fetched else 0,
        "captured": {"phones": r["with_phone"] or 0, "contact_forms": r["with_form"] or 0,
                     "linkedin": r["with_linkedin"] or 0},
        "unique_domains": r["unique_domains"] or 0,
        "done": done,
        "running": r["running"] or 0,
        "retrying": r["retrying"] or 0,
        "failed": r["failed"] or 0,
        "found": found,
        "cached": r["cached"] or 0,
        "jina_tokens": int(r["jina_tokens"] or 0),
        "hit_rate_so_far": found / done if done else 0.0,
    }


async def stream_rows(session: AsyncSession, job: Job):
    """Yield (raw_row, DomainResult|None, pending_domain, retrying) in input order.

    Reads the result snapshot on job_domains, never domains.result: the shared cache
    moves on when a later job refetches, and a finished job's download must not.
    Uses a server-side cursor: buffering 50k rows before the first byte defeats the
    point of streaming.
    """
    stmt = (
        select(JobItem, JobDomain.state, JobDomain.attempts, JobDomain.result)
        .outerjoin(
            JobDomain,
            (JobDomain.job_id == JobItem.job_id) & (JobDomain.domain == JobItem.domain),
        )
        .where(JobItem.job_id == job.id)
        .order_by(JobItem.row_index)
        .execution_options(yield_per=500)
    )
    # Bounded: rows come in row order, not domain order, so an unbounded dict kept all
    # 50k parsed results alive for the whole download.
    parsed: OrderedDict[str, DomainResult | None] = OrderedDict()
    async for item, state, attempts, raw in (await session.stream(stmt)):
        result = None
        if state == "done" and raw:
            if item.domain in parsed:
                parsed.move_to_end(item.domain)
                result = parsed[item.domain]
            else:
                try:
                    result = DomainResult.model_validate(raw)
                except Exception:  # noqa: BLE001
                    result = None
                parsed[item.domain] = result
                if len(parsed) > PARSED_CACHE:
                    parsed.popitem(last=False)
        if result is None and item.status == "invalid_input":
            result = DomainResult(
                domain=item.domain or item.input_value[:255] or "-",
                status="invalid_input",
                error_reason=item.reason,
            )
        retrying = result is None and state == "queued" and (attempts or 0) > 0
        yield item.raw_row or {}, result, item.domain, retrying
