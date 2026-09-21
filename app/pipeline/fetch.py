"""Fetch layer (spec section 6 Stage 4, with section 16 connections and section 17 errors).

Three deviations from the literal spec text, each verified against the live Jina API
on 2026-09-19 and each authorised by the Stage 4 milestone check. They are listed in
README under "Verified deviations":

1. Jina returns the page in `data.html` (html mode) / `data.text` (text mode).
   There is no `data.content`; reading it yields "" for every page.
2. `script:not([type="application/ld+json"])` in X-Remove-Selector strips *all*
   scripts, JSON-LD included. Per the milestone check's own fallback the selector
   is dropped and non-JSON-LD scripts are stripped in code instead.
3. `get_text` always uses httpx, even when FETCH_BACKEND=jina. Jina's text mode
   collapses newlines, and the spec's robots regex `(?im)^\\s*sitemap:` is line
   anchored -- through Jina it matches nothing, so sitemap discovery would silently
   find zero sitemaps on every domain.
"""

import asyncio
import email.utils
import logging
import re
from enum import Enum
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from app.ratelimit import CircuitOpen, FetchGate, jittered
from app.settings import settings

log = logging.getLogger("email_extractor.fetch")

JINA_BASE = "https://r.jina.ai/"

# Spec 15: a bot-challenge page must never be extracted from.
BLOCK_MARKERS = ("just a moment", "attention required", "cf-chl", "captcha", "access denied")

_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)
_JSONLD_RE = re.compile(r"""type\s*=\s*["']?application/ld\+json""", re.I)
_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style\s*>", re.I | re.S)
_SVG_RE = re.compile(r"<svg\b[^>]*>.*?</svg\s*>", re.I | re.S)


class ErrorClass(str, Enum):
    """Spec 17 error classes."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    BLOCKED = "blocked"
    PROVIDER_ACCOUNT = "provider_account"
    BUG = "bug"


class FetchError(Exception):
    def __init__(
        self,
        error_class: ErrorClass,
        reason: str,
        detail: str = "",
        status: int | None = None,
        retry_after: float | None = None,
        provider: bool = False,
    ):
        super().__init__(f"{error_class.value}/{reason}" + (f" ({detail})" if detail else ""))
        self.error_class = error_class
        self.reason = reason
        self.detail = detail
        self.status = status
        # Honoured by the retry loop in place of the standard backoff (spec 17 level 1).
        self.retry_after = retry_after
        # Spec 17 level 3 scopes the breaker to a PROVIDER. A dead business site is not
        # Jina being unhealthy, and must never contribute to opening it.
        self.provider = provider

    @property
    def retryable(self) -> bool:
        return self.error_class is ErrorClass.TRANSIENT


class FetchResult(BaseModel):
    url: str
    final_url: str
    status: int
    html: str
    bytes: int
    jina_tokens: int | None = None


class Fetcher(Protocol):
    async def get_html(self, url: str, *, domain: str | None = None) -> FetchResult: ...
    async def get_text(self, url: str, *, domain: str | None = None) -> FetchResult: ...


# --- helpers --------------------------------------------------------------


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def parse_retry_after(value: str | None, cap: float = 30.0) -> float | None:
    """Seconds or HTTP-date, capped (spec 17 level 1)."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, min(float(value), cap))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    import datetime as _dt

    now = _dt.datetime.now(_dt.UTC) if dt.tzinfo else _dt.datetime.now()
    return max(0.0, min((dt - now).total_seconds(), cap))


def strip_non_jsonld_scripts(html: str) -> str:
    """Remove <script> blocks except JSON-LD, plus <style> and <svg> (Stage 4, both backends)."""

    def repl(m: re.Match[str]) -> str:
        return m.group(0) if _JSONLD_RE.search(m.group(1) or "") else " "

    html = _SCRIPT_RE.sub(repl, html)
    html = _STYLE_RE.sub(" ", html)
    return _SVG_RE.sub(" ", html)


def looks_blocked(status: int, html: str) -> bool:
    """Spec 15 bot-challenge detection."""
    head = html[:4000].lower()
    if any(m in head for m in BLOCK_MARKERS):
        return True
    return status == 403


def classify_site_status(status: int, html: str = "") -> ErrorClass | None:
    """Classify the *target site's* HTTP status. None means success."""
    if 200 <= status < 300:
        return None
    if status in (403, 503) and looks_blocked(status, html):
        return ErrorClass.BLOCKED
    if status == 429 or 500 <= status <= 599:
        return ErrorClass.TRANSIENT
    if status == 403:
        return ErrorClass.BLOCKED
    return ErrorClass.PERMANENT


def classify_provider_status(status: int) -> ErrorClass | None:
    """Classify r.jina.ai's own HTTP status (spec 17)."""
    if 200 <= status < 300:
        return None
    if status in (401, 402):
        return ErrorClass.PROVIDER_ACCOUNT
    if status == 403:
        # Verified live: r.jina.ai answers 403 when the TARGET site refuses it, not when
        # our account is bad. Treating it as an account error paused the entire job on
        # the first bot-protected site; a real key or quota problem surfaces as 401/402.
        return ErrorClass.BLOCKED
    if status == 409:
        # X-Token-Budget exceeded: the page is too expensive to be worth extracting.
        return ErrorClass.PERMANENT
    if status == 422:
        # DEVIATION: 422 is absent from the spec 17 table, so it would fall through
        # to the Bug class and feed the internal_error alarm that pauses a job at 2%.
        # Verified permanent: "unexpected content type application/octet-stream".
        return ErrorClass.PERMANENT
    if status == 429 or 500 <= status <= 599:
        return ErrorClass.TRANSIENT
    return ErrorClass.PERMANENT


def decode_body(raw: bytes, response: httpx.Response) -> str:
    """Declared charset, then utf-8, then cp1252, then utf-8 with replacement (spec 15)."""
    for enc in (response.charset_encoding, "utf-8", "cp1252"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _exc_to_fetch_error(exc: Exception, *, provider: bool = False) -> FetchError:
    """`provider` says whether this transport failure was talking to the PROVIDER.

    It was previously accepted and ignored, so every timeout against r.jina.ai recorded
    as a breaker success while target-side 409/422 recorded as failures -- exactly
    backwards.
    """
    if isinstance(exc, httpx.TooManyRedirects):
        return FetchError(ErrorClass.PERMANENT, "redirect_loop", type(exc).__name__)
    if isinstance(exc, httpx.ConnectError) and "certificate" in str(exc).lower():
        return FetchError(ErrorClass.PERMANENT, "ssl_error", str(exc)[:120])
    if isinstance(exc, (httpx.TimeoutException, httpx.HTTPError)):
        return FetchError(
            ErrorClass.TRANSIENT, "homepage", type(exc).__name__, provider=provider
        )
    return FetchError(ErrorClass.BUG, "internal_error", f"{type(exc).__name__}: {exc}"[:200])


# --- shared client (spec 16: one per worker process, keep-alive) ----------

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            http2=False,
            follow_redirects=True,
            max_redirects=5,
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_s,
                read=settings.read_timeout_s,
                write=settings.read_timeout_s,
                pool=settings.connect_timeout_s,
            ),
            limits=httpx.Limits(
                max_connections=settings.global_fetch_concurrency,
                max_keepalive_connections=100,
            ),
            headers={"User-Agent": settings.user_agent},
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# --- base -----------------------------------------------------------------


class _BaseFetcher:
    """Shared retry loop. Spec 17 level 1: 2 retries, 1s and 3s, each +0-50% jitter."""

    RETRY_DELAYS = (1.0, 3.0)
    provider_backed = False        # httpx talks to business sites, not to a provider

    def __init__(self, gate: FetchGate) -> None:
        self.gate = gate

    async def _attempt_with_retries(self, url: str, domain: str, fn) -> FetchResult:
        last: FetchError | None = None
        for attempt in range(len(self.RETRY_DELAYS) + 1):
            try:
                async with self.gate.slot(domain):
                    result = await fn()
                if self.provider_backed:
                    self.gate.breaker.record(True)
                return result
            except CircuitOpen:
                raise
            except FetchError as e:
                last = e
                if self.provider_backed:
                    self.gate.breaker.record(not e.provider, e.detail or e.reason)
                if not e.retryable or attempt >= len(self.RETRY_DELAYS):
                    raise
                await asyncio.sleep(e.retry_after or jittered(self.RETRY_DELAYS[attempt]))
            except Exception as e:  # noqa: BLE001
                err = _exc_to_fetch_error(e, provider=self.provider_backed)
                last = err
                if self.provider_backed:
                    self.gate.breaker.record(not err.provider, err.detail or err.reason)
                if not err.retryable or attempt >= len(self.RETRY_DELAYS):
                    raise err from e
                await asyncio.sleep(jittered(self.RETRY_DELAYS[attempt]))
        assert last is not None
        raise last

    async def _raw_get(self, url: str, headers: dict[str, str] | None = None) -> tuple[httpx.Response, bytes]:
        """GET with a hard byte cap (spec 15: truncate at MAX_PAGE_BYTES, still extract)."""
        client = get_client()
        async with client.stream("GET", url, headers=headers) as r:
            chunks: list[bytes] = []
            total = 0
            async for chunk in r.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= settings.max_page_bytes:
                    break
            return r, b"".join(chunks)[: settings.max_page_bytes]


# --- httpx backend --------------------------------------------------------


class HttpxFetcher(_BaseFetcher):
    """Direct fetching. Also serves get_text for every backend (deviation 3)."""

    async def get_html(self, url: str, *, domain: str | None = None) -> FetchResult:
        d = domain or host_of(url)

        async def go() -> FetchResult:
            r, raw = await self._raw_get(url)
            ctype = r.headers.get("content-type", "")
            body = decode_body(raw, r)
            cls = classify_site_status(r.status_code, body)
            if cls is not None:
                reason = "blocked" if cls is ErrorClass.BLOCKED else "homepage"
                raise FetchError(cls, reason, f"HTTP {r.status_code}", r.status_code)
            if ctype and "html" not in ctype.lower() and "xml" not in ctype.lower():
                raise FetchError(ErrorClass.PERMANENT, "not_html", ctype, r.status_code)
            if looks_blocked(r.status_code, body):
                raise FetchError(ErrorClass.BLOCKED, "blocked", "challenge page", r.status_code)
            return FetchResult(
                url=url,
                final_url=str(r.url),
                status=r.status_code,
                html=strip_non_jsonld_scripts(body),
                bytes=len(raw),
            )

        return await self._attempt_with_retries(url, d, go)

    async def get_text(self, url: str, *, domain: str | None = None) -> FetchResult:
        d = domain or host_of(url)

        async def go() -> FetchResult:
            r, raw = await self._raw_get(url)
            body = decode_body(raw, r)
            cls = classify_site_status(r.status_code, body)
            if cls is not None:
                raise FetchError(cls, "not_html", f"HTTP {r.status_code}", r.status_code)
            return FetchResult(
                url=url, final_url=str(r.url), status=r.status_code, html=body, bytes=len(raw)
            )

        return await self._attempt_with_retries(url, d, go)


# --- jina backend ---------------------------------------------------------


class JinaFetcher(_BaseFetcher):
    """Reader API. get_text delegates to httpx (deviation 3)."""

    provider_backed = True

    def __init__(self, gate: FetchGate) -> None:
        super().__init__(gate)
        self._text = HttpxFetcher(gate)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.jina_api_key}",
            "Accept": "application/json",
            "X-Engine": "direct",
            "X-Respond-With": "html",
            "X-Retain-Images": "none",
            # X-Timeout is Jina's own deadline; keep it inside our client timeout.
            "X-Timeout": str(settings.connect_timeout_s + settings.read_timeout_s),
            # DEVIATION 2: the spec's script:not(...) clause strips JSON-LD too.
            "X-Remove-Selector": (
                "style, svg, img, iframe, noscript, link, meta, "
                ".swiper, .slick-slider, .owl-carousel"
            ),
            # DEVIATION 5: return only the regions that carry contact details. Measured
            # over 30 real roofer homepages this is 3x cheaper than the whole page
            # (9,428 vs 28,275 mean tokens) and finds exactly the same emails. The
            # entries after the footer/contact block are what make it lossless: without
            # them three of eighteen addresses disappeared.
            "X-Target-Selector": (
                'footer, [id*="footer" i], [class*="footer" i], a[href^="mailto:"], '
                '[class*="contact" i], [id*="contact" i], header, '
                '[class*="topbar" i], [class*="top-bar" i], [data-cfemail], '
                'a[href*="email-protection"], a[href^="tel:"], '
                'script[type="application/ld+json"]'
            ),
            # DEVIATION 6: a server-side page cost ceiling. Jina answers 409 and bills
            # NOTHING when a page would exceed it, so one bloated page can no longer
            # dominate a job's spend (one homepage measured 81,817 tokens alone).
            "X-Token-Budget": str(settings.max_page_tokens),
        }

    async def get_html(self, url: str, *, domain: str | None = None) -> FetchResult:
        d = domain or host_of(url)

        async def go() -> FetchResult:
            client = get_client()
            r = await client.get(JINA_BASE + url, headers=self._headers())
            cls = classify_provider_status(r.status_code)
            if cls is not None:
                if cls is ErrorClass.TRANSIENT and r.status_code == 429:
                    # A 429 means WE are going too fast, not that Jina is unhealthy.
                    # Slow the bucket; do NOT feed the breaker. Counting it as provider
                    # failure opened the circuit, which then made every waiting domain
                    # sit 30s and expire as domain_timeout -- a self-inflicted stall.
                    if self.gate.bucket:
                        self.gate.bucket.penalize(0.25, 60.0)
                    raise FetchError(
                        cls, "homepage", "jina 429", 429, provider=False,
                        retry_after=parse_retry_after(r.headers.get("retry-after")),
                    )
                permanent_reason = {409: "page_too_large", 422: "not_html"}.get(
                    r.status_code, "homepage"
                )
                reason = {
                    ErrorClass.PROVIDER_ACCOUNT: "jina_account",
                    ErrorClass.BLOCKED: "blocked",
                    ErrorClass.PERMANENT: permanent_reason,
                }.get(cls, "homepage")
                if r.status_code == 409:
                    log.warning(
                        "page_over_token_budget",
                        extra={"url": url, "budget": settings.max_page_tokens},
                    )
                raise FetchError(
                    cls, reason, f"jina HTTP {r.status_code}", r.status_code,
                    # Only genuine provider trouble may move the breaker. 409 is our
                    # own token budget refusing a page and 422 is the target serving a
                    # non-HTML content type -- neither means Jina is unhealthy.
                    provider=cls in (ErrorClass.TRANSIENT, ErrorClass.PROVIDER_ACCOUNT),
                )

            try:
                payload = r.json()
            except ValueError as e:
                raise FetchError(
                    ErrorClass.TRANSIENT, "homepage", "jina non-json body", provider=True
                ) from e

            data = payload.get("data")
            if not isinstance(data, dict):
                # Spec 15: 200 with an error payload counts as a failed fetch.
                raise FetchError(
                    ErrorClass.TRANSIENT, "homepage", f"jina payload {payload.get('code')}",
                    provider=True,
                )

            # DEVIATION 1: data.html, not data.content.
            html = data.get("html") or ""
            site_status = int(data.get("httpStatus") or 200)
            site_cls = classify_site_status(site_status, html)
            if site_cls is not None:
                reason = "blocked" if site_cls is ErrorClass.BLOCKED else "homepage"
                raise FetchError(site_cls, reason, f"site HTTP {site_status}", site_status)
            if not html.strip():
                raise FetchError(ErrorClass.TRANSIENT, "homepage", "jina empty html")
            if looks_blocked(site_status, html):
                raise FetchError(ErrorClass.BLOCKED, "blocked", "challenge page", site_status)

            usage = data.get("usage") or {}
            return FetchResult(
                url=url,
                final_url=data.get("url") or url,
                status=site_status,
                html=strip_non_jsonld_scripts(html)[: settings.max_page_bytes],
                bytes=len(html),
                jina_tokens=usage.get("tokens"),
            )

        return await self._attempt_with_retries(url, d, go)

    async def get_text(self, url: str, *, domain: str | None = None) -> FetchResult:
        return await self._text.get_text(url, domain=domain)


# --- factory --------------------------------------------------------------


def build_gate() -> FetchGate:
    return FetchGate(
        rate_per_minute=settings.jina_rpm if settings.fetch_backend == "jina" else None,
        global_concurrency=settings.global_fetch_concurrency,
        per_domain_concurrency=settings.per_domain_concurrency,
        provider=settings.fetch_backend,
    )


def build_fetcher(gate: FetchGate | None = None) -> Fetcher:
    gate = gate or build_gate()
    if settings.fetch_backend == "jina":
        return JinaFetcher(gate)
    return HttpxFetcher(gate)
