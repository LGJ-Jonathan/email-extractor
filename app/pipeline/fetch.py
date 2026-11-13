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
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel

from app import netguard
from app.pipeline.htmlscan import Block, looks_emailish, strip_blocks
from app.ratelimit import CircuitOpen, FetchGate, jittered
from app import provider_keys
from app.settings import settings

log = logging.getLogger("email_extractor.fetch")

JINA_BASE = "https://r.jina.ai/"

# Spec 15: a bot-challenge page must never be extracted from. The text markers are
# matched against what the page SHOWS, not its markup: "captcha" in the raw HTML
# matched every homepage that loads reCAPTCHA for its contact form (a `<script
# src=".../recaptcha/api.js">` in the head, or a `g-recaptcha` class), and a blocked
# homepage fails the whole domain. "cf-chl" is a Cloudflare challenge attribute, so it
# alone is still looked for in the markup.
BLOCK_TEXT_MARKERS = ("just a moment", "attention required", "captcha", "access denied")
BLOCK_MARKUP_MARKERS = ("cf-chl",)
BLOCK_MARKERS = BLOCK_TEXT_MARKERS + BLOCK_MARKUP_MARKERS

_JSONLD_RE = re.compile(r"""type\s*=\s*["']?application/ld\+json""", re.I)
_TAG_RE = re.compile(r"<[^>]*>")


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
    # How the page was obtained: direct (our own fetch), jina (Jina's plain fetch),
    # jina_render (Jina's headless browser, JavaScript executed).
    via: str = "direct"


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


def _keep_script(b: Block, body: str) -> bool:
    """JSON-LD always; any other inline script only when it carries an address.

    Dropping every script threw away `var email = "info@acme.com"` and the
    `document.write` obfuscations, so the "script" extraction source only ever saw
    failed JSON-LD. Kept scripts never reach the visible text (visible_text removes
    them), so they can add an address but not pollute a context window.
    """
    return bool(_JSONLD_RE.search(b.attrs)) or looks_emailish(body)


def strip_non_jsonld_scripts(html: str) -> str:
    """Remove <script> blocks except JSON-LD and address-bearing ones, plus <style> and
    <svg> (Stage 4, both backends). Linear in the page size."""
    html = strip_blocks(html, "script", keep=_keep_script)
    html = strip_blocks(html, "style")
    return strip_blocks(html, "svg")


def looks_blocked(status: int, html: str) -> bool:
    """Spec 15 bot-challenge detection."""
    head = html[:8000]
    low = head.lower()
    if any(m in low for m in BLOCK_MARKUP_MARKERS):
        return True
    shown = _TAG_RE.sub(" ", strip_blocks(strip_blocks(head, "script"), "style")).lower()
    if any(m in shown for m in BLOCK_TEXT_MARKERS):
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


# Failures that genuinely say something about the PROVIDER's health. r.jina.ai is a
# proxy: it fetches the business site for us, so a slow site on cheap shared hosting
# makes OUR request to Jina slow. Only a failure that happened before Jina accepted the
# request -- connect, TLS handshake, exhausted connection pool -- is evidence about
# Jina. A ReadTimeout after a successful connect is the target rendering slowly, and
# counting those as provider failures is what opened the breaker on 3,095 domains in
# the 16k run; none of them were Jina's fault and the retry pass found 54% of them.
#
# PoolTimeout is NOT here. It means OUR connection pool was full, which says nothing
# about Jina. It used to count, and the Jina and direct fetches shared one 50-connection
# pool: robots.txt and sitemap fetches to slow business sites held every connection,
# the Jina calls waited 3 s for one and gave up, and each give-up was booked as Jina
# being down. On the 18k Miami run that opened the breaker 627 times (every trip's
# reasons read {"PoolTimeout": 5}), cut throughput to ~230 domains an hour and wrote
# off 1,760 domains as circuit_open without one request reaching Jina.
PROVIDER_HEALTH_EXC = (httpx.ConnectError, httpx.ConnectTimeout)


def _exc_to_fetch_error(exc: Exception, *, provider: bool = False) -> FetchError:
    """`provider` says whether this transport failure was talking to the PROVIDER.

    It was previously accepted and ignored, so every timeout against r.jina.ai recorded
    as a breaker success while target-side 409/422 recorded as failures -- exactly
    backwards. Now it is honoured, but only for the exception types above: "we were
    talking to Jina" is necessary for blaming Jina, not sufficient.
    """
    if isinstance(exc, httpx.TooManyRedirects):
        return FetchError(ErrorClass.PERMANENT, "redirect_loop", type(exc).__name__)
    if isinstance(exc, httpx.ConnectError) and "certificate" in str(exc).lower():
        return FetchError(ErrorClass.PERMANENT, "ssl_error", str(exc)[:120])
    if isinstance(exc, httpx.PoolTimeout):
        # Our own pool was full: retry, and never blame the provider for it.
        return FetchError(ErrorClass.TRANSIENT, "homepage", "PoolTimeout", provider=False)
    if isinstance(exc, (httpx.TimeoutException, httpx.HTTPError)):
        return FetchError(
            ErrorClass.TRANSIENT,
            "homepage",
            type(exc).__name__,
            provider=provider and isinstance(exc, PROVIDER_HEALTH_EXC),
        )
    return FetchError(ErrorClass.BUG, "internal_error", f"{type(exc).__name__}: {exc}"[:200])


# --- private-address guard (direct fetches only; Jina fetches from its own network) --

MAX_REDIRECTS = 5
REDIRECT_STATUSES = (301, 302, 303, 307, 308)


async def guard_url(url: str) -> None:
    """Refuse a URL whose host resolves to any non-public address.

    The check and the connect are two lookups, so a DNS-rebinding record with a zero
    TTL could still slip between them; pinning the checked address is not done here
    because a pinned connection to a shared IP (every Cloudflare-fronted site) would
    be reused for other hosts with the wrong TLS name. That window is closed after
    the connect instead: _raw_get checks the address it actually reached before it
    reads a byte of the body.

    A lookup that FAILS is refused too. It used to be waved through ("httpx will
    report it"), but httpx does its own lookup, and a guard that cannot see the
    answer must not assume it is public: the platform resolver does drop queries
    under load, which is exactly when that mattered.
    """
    if not settings.fetch_guard_private_addresses:
        return
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        return
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return
    try:
        await netguard.resolve_public(host, port)
    except netguard.PrivateAddress as e:
        raise FetchError(ErrorClass.PERMANENT, "private_address", host) from e
    except OSError as e:
        raise FetchError(ErrorClass.TRANSIENT, "homepage", "address check failed") from e


def _check_peer(response: httpx.Response) -> None:
    """Refuse a response that came from a non-public address (DNS rebinding: the
    name answered publicly for the guard and privately for the connect)."""
    if not settings.fetch_guard_private_addresses:
        return
    stream = response.extensions.get("network_stream")
    if stream is None:
        return
    addr = stream.get_extra_info("server_addr")
    if not addr:
        return
    import ipaddress

    try:
        ip = ipaddress.ip_address(str(addr[0]).split("%")[0])
    except ValueError:
        return
    if not netguard.is_public(ip):
        raise FetchError(ErrorClass.PERMANENT, "private_address", response.url.host)


def _inflate(raw: bytes, encoding: str, limit: int) -> bytes:
    """Decompress at most `limit` bytes of output.

    httpx's own decoding has no output limit: one 64 KB gzip chunk can inflate to
    tens of megabytes before a byte count on the decoded stream is ever consulted,
    and stacked encodings multiply that. So the body is read raw, capped compressed,
    and inflated here with a ceiling."""
    import zlib

    encoding = encoding.strip().lower()
    if encoding in ("", "identity"):
        return raw[:limit]
    if encoding in ("gzip", "x-gzip"):
        modes = (16 + zlib.MAX_WBITS,)
    elif encoding == "deflate":
        modes = (zlib.MAX_WBITS, -zlib.MAX_WBITS)      # zlib-wrapped, then raw
    else:
        # br/zstd are never asked for, and a list ("gzip, gzip") is a bomb's shape.
        raise FetchError(ErrorClass.PERMANENT, "not_html", f"content-encoding {encoding[:30]}")
    for wbits in modes:
        try:
            return zlib.decompressobj(wbits).decompress(raw, limit)
        except zlib.error:
            continue
    raise FetchError(ErrorClass.PERMANENT, "not_html", "undecodable body")


# --- shared client (spec 16: one per worker process, keep-alive) ----------

# Two clients, two pools. Direct fetches go to thousands of business sites, many slow
# or dead; provider fetches all go to one host. Sharing a pool let the first starve
# the second (see PROVIDER_HEALTH_EXC).
_client: httpx.AsyncClient | None = None
_provider_client: httpx.AsyncClient | None = None

# Waiting for a free connection is queueing, not failure: the gate's semaphore already
# bounds how many requests are in flight, so a wait here is short and must not error.
POOL_WAIT_S = 30.0


def _new_client(*, max_connections: int, keepalive: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=False,
        follow_redirects=False,      # _raw_get follows by hand, guarding each hop
        timeout=httpx.Timeout(
            connect=settings.connect_timeout_s,
            read=settings.read_timeout_s,
            write=settings.read_timeout_s,
            pool=POOL_WAIT_S,
        ),
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=keepalive,
        ),
        # Only what _inflate can decode with a ceiling.
        headers={"User-Agent": settings.user_agent, "Accept-Encoding": "gzip, deflate"},
    )


def get_client() -> httpx.AsyncClient:
    """Direct fetches to business sites (robots.txt, sitemaps, the httpx backend)."""
    global _client
    if _client is None or _client.is_closed:
        # Headroom over the gate's concurrency: a connection being torn down still
        # counts against the pool for a moment after its slot is released.
        n = settings.global_fetch_concurrency
        _client = _new_client(max_connections=n + max(10, n // 5), keepalive=20)
    return _client


def get_provider_client() -> httpx.AsyncClient:
    """Calls to the fetch provider (r.jina.ai): one host, so keep-alive pays."""
    global _provider_client
    if _provider_client is None or _provider_client.is_closed:
        n = settings.global_fetch_concurrency
        _provider_client = _new_client(max_connections=n + max(10, n // 5), keepalive=n)
    return _provider_client


async def close_client() -> None:
    global _client, _provider_client
    for c in (_client, _provider_client):
        if c is not None and not c.is_closed:
            await c.aclose()
    _client = None
    _provider_client = None


# --- base -----------------------------------------------------------------


class _BaseFetcher:
    """Shared retry loop. Spec 17 level 1: 2 retries, 1s and 3s, each +0-50% jitter."""

    RETRY_DELAYS = (1.0, 3.0)
    provider_backed = False        # httpx talks to business sites, not to a provider

    def __init__(self, gate: FetchGate) -> None:
        self.gate = gate

    async def _attempt_with_retries(self, url: str, domain: str, fn,
                                    retries: bool = True) -> FetchResult:
        """`retries=False` makes one attempt: for a fetch that has a fallback behind
        it, or is itself an optional extra, where three slow tries cost more than
        the page is worth."""
        last: FetchError | None = None
        delays = self.RETRY_DELAYS if retries else ()
        for attempt in range(len(delays) + 1):
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
                if not e.retryable or attempt >= len(delays):
                    raise
                await asyncio.sleep(e.retry_after or jittered(delays[attempt]))
            except Exception as e:  # noqa: BLE001
                err = _exc_to_fetch_error(e, provider=self.provider_backed)
                last = err
                # A full pool of OUR connections is no evidence either way about the
                # provider: not a failure, and not a successful half-open probe.
                if self.provider_backed and not isinstance(e, httpx.PoolTimeout):
                    self.gate.breaker.record(not err.provider, err.detail or err.reason)
                if not err.retryable or attempt >= len(delays):
                    raise err from e
                await asyncio.sleep(jittered(delays[attempt]))
        assert last is not None
        raise last

    async def _raw_get(self, url: str, headers: dict[str, str] | None = None) -> tuple[httpx.Response, bytes]:
        """GET with a hard byte cap (spec 15: truncate at MAX_PAGE_BYTES, still extract).

        Redirects are followed by hand so that every hop, not only the first URL, is
        checked against the private-address guard: a business site that 302s to
        `http://[fd12::1]:8080/` would otherwise be fetched from inside the private
        network and its body mined for addresses.
        """
        client = get_client()
        for _hop in range(MAX_REDIRECTS + 1):
            await guard_url(url)
            async with client.stream("GET", url, headers=headers, follow_redirects=False) as r:
                location = r.headers.get("location")
                if r.status_code in REDIRECT_STATUSES and location:
                    url = urljoin(url, location)
                    continue
                _check_peer(r)
                chunks: list[bytes] = []
                total = 0
                async for chunk in r.aiter_raw():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= settings.max_page_bytes:
                        break
                raw = b"".join(chunks)[: settings.max_page_bytes]
                return r, _inflate(raw, r.headers.get("content-encoding", ""),
                                   settings.max_page_bytes)
        raise httpx.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects")


# --- httpx backend --------------------------------------------------------


class HttpxFetcher(_BaseFetcher):
    """Direct fetching. Also serves get_text for every backend (deviation 3)."""

    # Extra request headers for page fetches (hybrid sends a browser's). Per fetcher,
    # not on the shared client, which TypeSafe calls also go through.
    page_headers: dict[str, str] | None = None

    async def get_html(self, url: str, *, domain: str | None = None,
                       retry: bool = True) -> FetchResult:
        d = domain or host_of(url)

        async def go() -> FetchResult:
            r, raw = await self._raw_get(url, self.page_headers)
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

        return await self._attempt_with_retries(url, d, go, retries=retry)

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

    # Our client deadline must sit OUTSIDE Jina's own (X-Timeout), not inside it. It
    # was connect+read = 10s against an X-Timeout of 10s, so the client abandoned the
    # request at the read timeout of 7s and Jina's own answer -- which says whether the
    # target was slow, blocked or non-HTML -- was never read. Every slow page became
    # our ReadTimeout instead of its classified error. This margin covers Jina's
    # response overhead once it has stopped waiting on the target.
    CLIENT_MARGIN_S = 5.0

    def __init__(self, gate: FetchGate, direct_gate: FetchGate | None = None) -> None:
        super().__init__(gate)
        self._text = HttpxFetcher(direct_gate or build_direct_gate())

    def _timeout(self, render: bool = False) -> httpx.Timeout:
        """Per-request, so direct target fetches keep the tight shared-client budget."""
        inner = settings.jina_render_timeout_s if render else settings.jina_timeout_s
        outer = inner + self.CLIENT_MARGIN_S
        return httpx.Timeout(
            connect=settings.connect_timeout_s,
            read=outer,
            write=outer,
            pool=POOL_WAIT_S,
        )

    def _headers(self, key: str, render: bool = False) -> dict[str, str]:
        # Plain fetch by default. A rendered fetch runs the page in Jina's headless
        # browser, which is Jina's default engine: the header is left out unless
        # JINA_RENDER_ENGINE names one.
        engine = settings.jina_render_engine if render else "direct"
        timeout_s = settings.jina_render_timeout_s if render else settings.jina_timeout_s
        h = {
            **({"X-Engine": engine} if engine else {}),
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "X-Respond-With": "html",
            "X-Retain-Images": "none",
            # X-Timeout is Jina's own deadline and must expire FIRST, so that a slow
            # target comes back as Jina's classified error rather than our timeout.
            # settings.read_timeout_s belongs to direct site fetches, not to this one.
            "X-Timeout": str(timeout_s),
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
        if render:
            # A rendered fetch is the last look at a site where the trimmed regions
            # showed no address, so it reads the whole page. On 260 real sites, 34 of
            # 59 "no contact info" results had an address outside those regions.
            del h["X-Target-Selector"]
        return h

    async def get_html(self, url: str, *, domain: str | None = None,
                       render: bool = False) -> FetchResult:
        d = domain or host_of(url)

        async def go() -> FetchResult:
            client = get_provider_client()
            # One key per request, in rotation across the pool. A key that is rejected
            # or out of credit is benched and the next one tried, here rather than
            # through the retry loop: with several dead keys that loop's three
            # attempts ran out and the page was failed while good keys remained.
            for _ in range(len(provider_keys.entries("jina")) + 1):
                key = provider_keys.current("jina")
                r = await client.get(JINA_BASE + url, headers=self._headers(key, render),
                                     timeout=self._timeout(render), follow_redirects=True)
                cls = classify_provider_status(r.status_code)
                if cls is ErrorClass.PROVIDER_ACCOUNT and provider_keys.bench(
                    "jina", key, "rejected" if r.status_code == 401 else "empty"
                ):
                    continue            # another key can carry on
                break
            if cls is None:
                provider_keys.unbench(key)
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
                    # A spent or rejected KEY is ours to fix, not Jina being unwell: it
                    # pauses the job (or rotates keys) and must not also open the
                    # breaker, which then throttled the first minute after a top-up.
                    provider=cls is ErrorClass.TRANSIENT,
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
                via="jina_render" if render else "jina",
            )

        # A rendered fetch is an optional extra and slow: one attempt.
        return await self._attempt_with_retries(url, d, go, retries=not render)

    async def get_text(self, url: str, *, domain: str | None = None) -> FetchResult:
        return await self._text.get_text(url, domain=domain)


# --- hybrid backend -------------------------------------------------------


# Direct failures Jina may still get past: it fetches from another network, so a site
# that refuses or stalls on our address can answer it. Everything else (a 404, a PDF,
# a private address, a redirect loop) is the site's own answer and is the same there.
_FALLBACK_REASONS = frozenset({"blocked", "homepage", "ssl_error"})


def _is_shell(html: str) -> bool:
    """A page that shows almost no text: the content is drawn by JavaScript."""
    from app.pipeline.parked import visible_text

    return len(visible_text(html)) < settings.hybrid_thin_text_chars


class HybridFetcher:
    """Our own fetch first, Jina for what it could not get.

    Most business sites serve plain HTML to anyone, so paying a provider (and queueing
    for its rate limit) to fetch them buys nothing. Jina is kept for the two things a
    plain fetch from here cannot do: reach a site that refuses our address, and run a
    page's JavaScript.

    `gate` is the Jina gate, so the worker's breaker and health reporting see the
    provider exactly as they do with FETCH_BACKEND=jina.
    """

    def __init__(self, gate: FetchGate, direct_gate: FetchGate | None = None) -> None:
        self.gate = gate
        self.direct_gate = direct_gate or build_direct_gate()
        self._direct = HttpxFetcher(self.direct_gate)
        self._direct.page_headers = {
            "User-Agent": settings.direct_user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        self._jina = JinaFetcher(gate, self.direct_gate)
        self.stats = {"direct": 0, "jina": 0, "jina_render": 0}

    def _count(self, r: FetchResult) -> FetchResult:
        self.stats[r.via] = self.stats.get(r.via, 0) + 1
        return r

    async def get_html(self, url: str, *, domain: str | None = None) -> FetchResult:
        try:
            # One direct attempt: Jina is the retry.
            r = await self._direct.get_html(url, domain=domain, retry=False)
        except FetchError as e:
            if e.reason not in _FALLBACK_REASONS or e.status == 404:
                raise
            log.info("hybrid_fallback", extra={"url": url, "why": e.reason, "detail": e.detail})
            return self._count(await self._jina.get_html(url, domain=domain))
        if _is_shell(r.html):
            log.info("hybrid_fallback", extra={"url": url, "why": "js_shell"})
            try:
                return self._count(await self.render_html(url, domain=domain))
            except CircuitOpen:
                pass                # Jina is unwell; the page we have still counts
            except FetchError as e:
                # Every key spent must reach the worker, which pauses the job.
                # Anything else, keep the page we do have.
                if e.error_class is ErrorClass.PROVIDER_ACCOUNT:
                    raise
        return self._count(r)

    async def render_html(self, url: str, *, domain: str | None = None) -> FetchResult:
        """The page as a browser shows it (Jina's headless browser)."""
        return await self._jina.get_html(url, domain=domain, render=True)

    async def get_text(self, url: str, *, domain: str | None = None) -> FetchResult:
        return await self._direct.get_text(url, domain=domain)


# --- factory --------------------------------------------------------------


def build_direct_gate() -> FetchGate:
    """Concurrency only, no rate limit, no provider breaker: these are our own requests
    to business sites (robots.txt, sitemaps). They used to share the Jina gate, so
    each one spent a Jina rate-limit token -- two of the ~11 per domain -- and was
    refused whenever Jina's breaker was open."""
    return FetchGate(
        rate_per_minute=None,
        global_concurrency=settings.global_fetch_concurrency,
        per_domain_concurrency=settings.per_domain_concurrency,
        provider="direct",
    )


def build_gate() -> FetchGate:
    rpm = settings.jina_rpm if settings.fetch_backend in ("jina", "hybrid") else None
    bucket = None
    # JINA_RPM is per key, so the budget follows the number of usable keys.
    scale = (lambda: float(provider_keys.usable_count("jina"))) if (
        rpm and settings.jina_rpm_per_key) else None
    if rpm and settings.rate_limit_backend == "redis":
        from app.ratelimit import RedisTokenBucket

        bucket = RedisTokenBucket(settings.redis_url, rpm, name="jina",
                                  scale=scale)
    elif rpm:
        from app.ratelimit import TokenBucket

        bucket = TokenBucket(rpm, scale=scale)
    return FetchGate(
        rate_per_minute=rpm,
        global_concurrency=settings.global_fetch_concurrency,
        per_domain_concurrency=settings.per_domain_concurrency,
        provider="jina" if rpm else settings.fetch_backend,
        bucket=bucket,
    )


def build_fetcher(gate: FetchGate | None = None) -> Fetcher:
    gate = gate or build_gate()
    if settings.fetch_backend == "jina":
        return JinaFetcher(gate)
    if settings.fetch_backend == "hybrid":
        return HybridFetcher(gate)
    return HttpxFetcher(gate)
