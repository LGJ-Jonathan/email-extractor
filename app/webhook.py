"""Webhook delivery (spec 5). A webhook failure never fails a domain or a job.

Three things this module guards against, all found in review:
- SSRF. The worker sits on the compose network next to Postgres, Redis and the API,
  and on the host's LAN. A webhook_url is only accepted if every address it resolves
  to is public, and delivery connects to the address that was checked (not a second
  lookup an attacker's DNS could answer differently).
- Fan-out. One task and one HTTP client per finished domain meant a 50k-row cached job
  started tens of thousands of deliveries at once. WebhookSender is one client, a fixed
  pool of senders and a bounded queue, so a slow receiver slows the job, not the host.
- Forgery. With WEBHOOK_SECRET set, every delivery is HMAC-signed.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.settings import settings

log = logging.getLogger("email_extractor")

BACKOFF_S = (2.0, 8.0, 30.0)


class WebhookRejected(ValueError):
    pass


def _public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def resolve_public(url: str) -> tuple[str, str, int]:
    """Validate a webhook URL. Returns (host, ip, port); raises WebhookRejected."""
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise WebhookRejected("webhook_url must be an http(s) URL")
    if parts.scheme == "http" and not settings.allow_http_webhooks:
        raise WebhookRejected("webhook_url must use https")
    if parts.username or parts.password:
        raise WebhookRejected("webhook_url must not contain credentials")
    host = parts.hostname
    if not host:
        raise WebhookRejected("webhook_url has no host")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as e:
        raise WebhookRejected("webhook_url has an invalid port") from e

    try:
        literal = ipaddress.ip_address(host)
        addrs = [literal]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        except OSError as e:
            raise WebhookRejected("webhook_url host does not resolve") from e
        addrs = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addrs:
        raise WebhookRejected("webhook_url host does not resolve")
    # Every address, not just the first: a name with one public and one private A
    # record would otherwise pass the check and be delivered to the private one.
    if not all(_public(a) for a in addrs):
        raise WebhookRejected("webhook_url must point at a public address")
    return host, str(addrs[0]), port


async def validate_webhook_url(url: str | None) -> None:
    if url is not None:
        await resolve_public(url)


def sign(body: bytes, timestamp: int, secret: str) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


async def deliver(url: str, payload: dict, *, client: httpx.AsyncClient,
                  backoff: tuple[float, ...] = BACKOFF_S) -> bool:
    """POST once, then retry after each backoff step. Returns True on any 2xx."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    delivery_id = payload.get("delivery_id") or uuid.uuid4().hex
    for attempt, delay in enumerate((0.0, *backoff)):
        if delay:
            await asyncio.sleep(delay)
        try:
            host, ip, port = await resolve_public(url)
        except WebhookRejected as e:
            log.warning("webhook_rejected", extra={"job_id": payload.get("job_id"),
                                                   "err": str(e)})
            return False
        parts = urlsplit(url)
        ip_host = f"[{ip}]" if ":" in ip else ip
        pinned = urlunsplit((parts.scheme, f"{ip_host}:{port}", parts.path or "/",
                             parts.query, ""))
        ts = int(time.time())
        headers = {
            "Host": parts.netloc.rsplit("@", 1)[-1],
            "Content-Type": "application/json",
            "User-Agent": "email-extractor-webhook/1",
            "X-Extractor-Delivery": delivery_id,
            "X-Extractor-Event": str(payload.get("event", "")),
        }
        if settings.webhook_secret:
            headers["X-Extractor-Signature"] = sign(body, ts, settings.webhook_secret)
        try:
            r = await client.post(pinned, content=body, headers=headers,
                                  extensions={"sni_hostname": host})
            if 200 <= r.status_code < 300:
                return True
            err = f"http_{r.status_code}"
        except httpx.HTTPError as e:
            err = type(e).__name__
        log.warning("webhook_failed", extra={
            "job_id": payload.get("job_id"), "attempt": attempt + 1, "err": err,
        })
    return False


class WebhookSender:
    """A bounded pool of deliverers sharing one HTTP client."""

    def __init__(self, senders: int = 10, max_pending: int = 1000) -> None:
        self._queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(max_pending)
        self._senders = senders
        self._tasks: list[asyncio.Task] = []
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=settings.webhook_timeout_s,
                                         follow_redirects=False)
        self._tasks = [asyncio.create_task(self._run()) for _ in range(self._senders)]

    async def submit(self, url: str, payload: dict) -> None:
        """Waits when the queue is full: backpressure instead of unbounded tasks."""
        payload.setdefault("delivery_id", uuid.uuid4().hex)
        await self._queue.put((url, payload))

    async def _run(self) -> None:
        while True:
            url, payload = await self._queue.get()
            try:
                await deliver(url, payload, client=self._client)
            except Exception:  # noqa: BLE001 - one bad delivery must not stop the pool
                log.exception("webhook_crashed")
            finally:
                self._queue.task_done()

    async def close(self, drain_s: float = 10.0) -> None:
        """Give queued deliveries a short window on shutdown, then report what is lost."""
        try:
            await asyncio.wait_for(self._queue.join(), drain_s)
        except TimeoutError:
            log.warning("webhooks_dropped_on_shutdown", extra={"pending": self._queue.qsize()})
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
