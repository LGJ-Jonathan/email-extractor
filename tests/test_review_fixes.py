"""Fixes from the 71ee797 review that need no database."""

import asyncio
import hashlib
import hmac
import time
from unittest.mock import patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import webhook
from app.main import app
from app.settings import settings

client = TestClient(app, raise_server_exceptions=False)


# --- webhook address checks (SSRF) ----------------------------------------


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/x",
    "https://[::1]/x",
    "https://169.254.169.254/latest/meta-data",
    "https://10.1.2.3/x",
    "https://192.168.1.10/x",
    "https://172.18.0.2/x",               # a compose network address
    "https://100.99.177.106/x",           # Tailscale CGNAT range
    "https://[::ffff:127.0.0.1]/x",
    "https://0.0.0.0/x",
    "https://localhost/x",
    "http://example.com/x",               # plain http, off by default
    "ftp://example.com/x",
    "https://user:pw@example.com/x",
    "https:///nohost",
])
async def test_non_public_webhook_urls_are_rejected(url):
    with pytest.raises(webhook.WebhookRejected):
        await webhook.resolve_public(url)


async def test_a_name_with_any_private_address_is_rejected():
    fake = [(2, 1, 6, "", ("93.184.215.14", 443)), (2, 1, 6, "", ("10.0.0.7", 443))]

    async def getaddrinfo(*a, **k):
        return fake

    loop = asyncio.get_running_loop()
    with patch.object(loop, "getaddrinfo", getaddrinfo):
        with pytest.raises(webhook.WebhookRejected):
            await webhook.resolve_public("https://mixed.example/hook")


async def test_public_address_passes():
    host, ip, port = await webhook.resolve_public("https://93.184.215.14:8443/hook")
    assert (host, ip, port) == ("93.184.215.14", "93.184.215.14", 8443)


async def test_http_allowed_only_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "allow_http_webhooks", True)
    assert (await webhook.resolve_public("http://93.184.215.14/x"))[2] == 80


# --- delivery ------------------------------------------------------------


@respx.mock
async def test_delivery_connects_to_the_checked_ip_with_the_real_host_and_signature(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", "s3cret")

    async def resolved(url):
        return "hooks.example.com", "93.184.215.14", 443

    monkeypatch.setattr(webhook, "resolve_public", resolved)
    route = respx.post("https://93.184.215.14:443/in").mock(return_value=httpx.Response(204))
    async with httpx.AsyncClient() as c:
        ok = await webhook.deliver("https://hooks.example.com/in",
                                   {"event": "domain.done", "job_id": "j"}, client=c)
    assert ok
    req = route.calls.last.request
    assert req.headers["host"] == "hooks.example.com"
    assert req.extensions.get("sni_hostname") == "hooks.example.com"
    t, v1 = (p.split("=", 1)[1] for p in req.headers["x-extractor-signature"].split(","))
    expected = hmac.new(b"s3cret", f"{t}.".encode() + req.content, hashlib.sha256).hexdigest()
    assert v1 == expected and abs(int(t) - time.time()) < 5
    assert req.headers["x-extractor-event"] == "domain.done"


@respx.mock
async def test_delivery_rechecks_the_address_on_every_attempt(monkeypatch):
    answers = iter([("h", "93.184.215.14", 443)])

    async def rebinding(url):
        try:
            return next(answers)
        except StopIteration:
            raise webhook.WebhookRejected("webhook_url must point at a public address")

    monkeypatch.setattr(webhook, "resolve_public", rebinding)
    respx.post("https://93.184.215.14:443/").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as c:
        ok = await webhook.deliver("https://h/", {"job_id": "j"}, client=c, backoff=(0.0,))
    assert ok is False
    assert respx.calls.call_count == 1          # the second attempt was refused, not sent


async def test_sender_is_bounded_and_applies_backpressure(monkeypatch):
    started = 0
    release = asyncio.Event()

    async def slow(url, payload, **kw):
        nonlocal started
        started += 1
        await release.wait()
        return True

    monkeypatch.setattr(webhook, "deliver", slow)
    sender = webhook.WebhookSender(senders=3, max_pending=5)
    await sender.start()
    for i in range(8):                          # 3 in flight + 5 queued
        await asyncio.wait_for(sender.submit("https://x", {"i": i}), 1)
    await asyncio.sleep(0.05)
    assert started == 3
    with pytest.raises(TimeoutError):           # the 9th waits: backpressure
        await asyncio.wait_for(sender.submit("https://x", {"i": 9}), 0.1)
    release.set()
    await sender.close(drain_s=2)
    assert started == 8


# --- API hardening -------------------------------------------------------


def test_malformed_json_is_400_not_500(api_key):
    r = client.post("/jobs", content=b"{not json",
                    headers={"X-API-Key": api_key, "content-type": "application/json"})
    assert r.status_code == 400


def test_wrong_shape_is_422_not_500(api_key):
    r = client.post("/jobs", json={"items": "acme.com"}, headers={"X-API-Key": api_key})
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["field"] == "items"


def test_extract_during_a_provider_account_error_is_503_not_500(api_key):
    from app.pipeline.fetch import ErrorClass, FetchError

    async def dead(*a, **k):
        raise FetchError(ErrorClass.PROVIDER_ACCOUNT, "provider_account", detail="402")

    with patch("app.main.process_input", dead):
        r = client.post("/extract", json={"url": "acme.com"}, headers={"X-API-Key": api_key})
    assert r.status_code == 503


def test_oversized_body_is_refused_by_declared_length(api_key):
    too_big = str(settings.max_upload_bytes + 2 * 1024 * 1024)
    r = client.post("/jobs", content=b"x",
                    headers={"X-API-Key": api_key, "content-length": too_big,
                             "content-type": "application/json"})
    assert r.status_code == 413


async def test_oversized_body_is_refused_while_streaming(monkeypatch, api_key):
    """No Content-Length (chunked): the limit trips as bytes arrive."""
    from app.main import BodySizeLimit

    seen = []

    async def inner(scope, receive, send):
        while True:
            msg = await receive()
            seen.append(len(msg.get("body", b"")))
            if not msg.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    chunks = [{"type": "http.request", "body": b"x" * 400, "more_body": True}] * 10
    it = iter(chunks)

    async def receive():
        return next(it)

    sent = []

    async def send(msg):
        sent.append(msg)

    await BodySizeLimit(inner, limit=1000)({"type": "http", "headers": []}, receive, send)
    assert sent[0]["status"] == 413
    assert sum(seen) <= 1200


def test_an_upload_over_the_cap_is_413(monkeypatch, api_key):
    monkeypatch.setattr(settings, "max_upload_bytes", 1000)
    files = {"file": ("big.csv", b"website\n" + b"a.com\n" * 500, "text/csv")}
    r = client.post("/jobs/preview", files=files, headers={"X-API-Key": api_key})
    assert r.status_code == 413


def test_placeholder_admin_key_is_refused(monkeypatch):
    for weak in ("change-me", "short-key"):
        monkeypatch.setattr(settings, "api_key", weak)
        r = client.post("/extract", json={"url": "x"}, headers={"X-API-Key": weak})
        assert r.status_code == 401, weak


async def test_redis_bucket_uses_socket_timeouts():
    from app.ratelimit import RedisTokenBucket

    b = RedisTokenBucket("redis://127.0.0.1:1/0", 600)
    b._redis()
    kw = b._client.connection_pool.connection_kwargs
    assert kw["socket_timeout"] == 1.0 and kw["socket_connect_timeout"] == 1.0
