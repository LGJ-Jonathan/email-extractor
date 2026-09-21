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


async def _jina(client: httpx.AsyncClient) -> dict:
    if not settings.jina_api_key:
        return {"status": "not_configured", "balance_tokens": None}
    try:
        r = await client.get(JINA_WALLET_URL, params={"api_key": settings.jina_api_key})
        wallet = (r.json() or {}).get("wallet") or {} if r.status_code == 200 else {}
        total = wallet.get("total_balance")
        balance = int(total) if isinstance(total, (int, float)) else None
    except (httpx.HTTPError, ValueError, TypeError):
        balance = None
    return {"status": jina_state(balance), "balance_tokens": balance}


async def _typesafe(client: httpx.AsyncClient) -> dict:
    if not settings.typesafe_api_key:
        return {"status": "not_configured"}
    try:
        r = await client.get(TYPESAFE_MODELS_URL,
                             headers={"Authorization": f"Bearer {settings.typesafe_api_key}"})
    except httpx.HTTPError:
        return {"status": "unknown"}
    if r.status_code == 200:
        return {"status": "ok"}
    if r.status_code in (401, 403):
        return {"status": "rejected"}
    if r.status_code == 402:
        return {"status": "empty"}
    return {"status": "unknown"}


_USAGE = text("""
SELECT coalesce(sum(jina_tokens), 0) AS jina_tokens,
       count(*) FILTER (WHERE typesafe_called) AS typesafe_calls,
       count(*) FILTER (WHERE NOT from_cache) AS domains_fetched
FROM job_domains
WHERE state = 'done' AND finished_at >= date_trunc('month', now())
""")


async def provider_status(session: AsyncSession) -> dict:
    global _cache
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
    }


def reset_cache() -> None:
    global _cache
    _cache = None
