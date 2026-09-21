"""Per-domain webhook delivery (spec 5). A webhook failure never fails the domain."""

import asyncio
import logging

import httpx

from app.settings import settings

log = logging.getLogger("email_extractor")

BACKOFF_S = (2.0, 8.0, 30.0)


def valid_webhook_url(url: str | None) -> bool:
    return url is None or url.startswith(("https://", "http://"))


async def deliver(url: str, payload: dict, *, client: httpx.AsyncClient | None = None,
                  backoff: tuple[float, ...] = BACKOFF_S) -> bool:
    """POST once, then retry after each backoff step. Returns True on any 2xx."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=settings.webhook_timeout_s)
    try:
        for attempt, delay in enumerate((0.0, *backoff)):
            if delay:
                await asyncio.sleep(delay)
            try:
                r = await client.post(url, json=payload)
                if 200 <= r.status_code < 300:
                    return True
                err = f"http_{r.status_code}"
            except httpx.HTTPError as e:
                err = type(e).__name__
            log.warning("webhook_failed", extra={
                "job_id": payload.get("job_id"), "attempt": attempt + 1, "err": err,
            })
        return False
    finally:
        if own:
            await client.aclose()
