"""Provider credit and key status for the portal's balance strip.

Jina: its dashboard's wallet endpoint (not in Jina's published API docs, so it may
change; any failure reads as status "unknown"). The response also carries the
account's email, billing address and payment method -- only the balance numbers
leave this module.

TypeSafe publishes no balance endpoint. GET /v1/models tells us whether the key
works; usage comes from our own records.

Results are cached per process for CACHE_S so page loads do not hit the vendors.
"""

import asyncio
import time
from datetime import UTC, datetime

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.provider_keys import current as current_key
from app.settings import settings

CACHE_S = 300
TIMEOUT_S = 6.0
JINA_WALLET_URL = "https://embeddings-dashboard-api.jina.ai/api/v1/api_key/user"
TYPESAFE_MODELS_URL = "https://api.typesafe.ai/v1/models"

_cache: tuple[float, dict] | None = None
_lock = asyncio.Lock()


def jina_state(balance: int | None) -> str:
    if balance is None:
        return "unknown"
    if balance <= 0:
        return "empty"
    return "low" if balance < settings.jina_low_balance_tokens else "ok"


async def jina_balance(client: httpx.AsyncClient, key: str) -> int | None:
    """The wallet's total balance for this key, or None if it can't be read."""
    try:
        r = await client.get(JINA_WALLET_URL, params={"api_key": key})
        wallet = (r.json() or {}).get("wallet") or {} if r.status_code == 200 else {}
        total = wallet.get("total_balance")
        return int(total) if isinstance(total, (int, float)) else None
    except (httpx.HTTPError, ValueError, TypeError):
        return None


async def typesafe_key_status(client: httpx.AsyncClient, key: str) -> str:
    try:
        r = await client.get(TYPESAFE_MODELS_URL, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError:
        return "unknown"
    return {200: "ok", 401: "rejected", 403: "rejected", 402: "empty"}.get(r.status_code, "unknown")


async def _jina(client: httpx.AsyncClient) -> dict:
    key = current_key("jina")
    if not key:
        return {"status": "not_configured", "balance_tokens": None}
    balance = await jina_balance(client, key)
    return {"status": jina_state(balance), "balance_tokens": balance}


async def _typesafe(client: httpx.AsyncClient) -> dict:
    key = current_key("typesafe")
    if not key:
        return {"status": "not_configured"}
    return {"status": await typesafe_key_status(client, key)}


_USAGE = text("""
SELECT coalesce(sum(jina_tokens), 0) AS jina_tokens,
       count(*) FILTER (WHERE typesafe_called) AS typesafe_calls,
       count(*) FILTER (WHERE NOT from_cache) AS domains_fetched
FROM job_domains
WHERE state = 'done' AND finished_at >= date_trunc('month', now())
""")


WORKER_SILENT_S = 120       # six missed heartbeats


async def provider_status(session: AsyncSession) -> dict:
    global _cache
    from app import provider_keys

    await provider_keys.refresh(session)
    now = time.monotonic()
    async with _lock:
        if _cache is None or now - _cache[0] > CACHE_S:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                jina, typesafe = await asyncio.gather(_jina(client), _typesafe(client))
            _cache = (now, {"jina": jina, "typesafe": typesafe,
                            "checked_at": datetime.now(UTC).isoformat()})
        vendors = _cache[1]
    usage = (await session.execute(_USAGE)).mappings().one()
    return {
        **vendors,
        "usage_this_month": {k: int(v or 0) for k, v in usage.items()},
        "jina_low_threshold": settings.jina_low_balance_tokens,
        "worker": await worker_health(session),
    }


async def worker_health(session: AsyncSession) -> dict:
    """Is the worker alive? Its heartbeat writes process_status every 20 s, so a row
    older than a couple of minutes means no worker is running, whatever the jobs say."""
    row = (await session.execute(text(
        "SELECT status, extract(epoch FROM now() - updated_at) AS age "
        "FROM process_status WHERE name = 'worker'"
    ))).first()
    if row is None:
        return {"status": "never_seen", "last_seen_s": None}
    age = int(row.age)
    health = (row.status or {}).get("health") or {}
    return {
        "status": "ok" if age < WORKER_SILENT_S else "silent",
        "last_seen_s": age,
        **{k: health.get(k) for k in ("in_flight", "concurrency", "last_claim_at", "breaker")},
    }


def cached_jina_status() -> str:
    """The last known Jina status, without calling Jina."""
    return (_cache[1]["jina"]["status"] if _cache else "unknown")


def reset_cache() -> None:
    global _cache
    _cache = None
