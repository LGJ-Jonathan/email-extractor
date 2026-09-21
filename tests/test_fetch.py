"""Fetch layer: Stage 4 milestone regressions and section 17 fault injection."""

import httpx
import pytest
import respx

from app.pipeline import fetch as F
from app.pipeline.fetch import (
    ErrorClass,
    FetchError,
    HttpxFetcher,
    JinaFetcher,
    classify_provider_status,
    classify_site_status,
    parse_retry_after,
    strip_non_jsonld_scripts,
)
from app.ratelimit import FetchGate

JSONLD = '<script type="application/ld+json">{"@type":"Organization","email":"a@b.com"}</script>'
PAGE = f'<html><head>{JSONLD}<script>var x=1;</script><style>b{{}}</style></head>' \
       '<body><a href="/contact">Contact</a><svg><path/></svg></body></html>'


@pytest.fixture(autouse=True)
async def _fresh_client():
    await F.close_client()
    yield
    await F.close_client()


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(F._BaseFetcher, "RETRY_DELAYS", (0.001, 0.002))


def gate() -> FetchGate:
    return FetchGate(
        rate_per_minute=None, global_concurrency=10, per_domain_concurrency=2, provider="jina"
    )


def jina_ok(html=PAGE, site_status=200, tokens=1234, url="https://acme.com/"):
    return httpx.Response(
        200,
        json={"code": 200, "data": {"html": html, "url": url, "httpStatus": site_status,
                                    "usage": {"tokens": tokens}}},
    )


# --- Stage 4 milestone regressions ---------------------------------------


def test_jsonld_survives_script_stripping():
    """Deviation 2: Jina's selector strips all scripts, so code must keep JSON-LD."""
    out = strip_non_jsonld_scripts(PAGE)
    assert "application/ld+json" in out
    assert '"email":"a@b.com"' in out
    assert "var x=1" not in out
    assert "<style" not in out
    assert "<svg" not in out
    assert 'href="/contact"' in out


@respx.mock
async def test_jina_reads_data_html_not_data_content():
    """Deviation 1: there is no data.content; reading it yields empty pages."""
    respx.get(url__startswith="https://r.jina.ai/").mock(return_value=jina_ok())
    r = await JinaFetcher(gate()).get_html("https://acme.com/")
    assert 'href="/contact"' in r.html
    assert r.jina_tokens == 1234
    assert r.final_url == "https://acme.com/"


@respx.mock
async def test_get_text_never_goes_through_jina():
    """Deviation 3: Jina text mode collapses newlines, breaking the robots regex."""
    jina = respx.get(url__startswith="https://r.jina.ai/").mock(return_value=jina_ok())
    direct = respx.get("https://acme.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nSitemap: https://acme.com/sitemap.xml\n")
    )
    r = await JinaFetcher(gate()).get_text("https://acme.com/robots.txt")
    assert direct.called and not jina.called
    assert "\n" in r.html          # newlines preserved -> the spec's regex can match
    import re
    assert re.findall(r"(?im)^\s*sitemap:\s*(\S+)", r.html) == ["https://acme.com/sitemap.xml"]


# --- section 17 fault injection ------------------------------------------


@respx.mock
async def test_timeouts_then_success_is_retried():
    route = respx.get(url__startswith="https://r.jina.ai/")
    route.side_effect = [httpx.TimeoutException("t"), httpx.TimeoutException("t"), jina_ok()]
    r = await JinaFetcher(gate()).get_html("https://acme.com/")
    assert r.status == 200
    assert route.call_count == 3


@respx.mock
async def test_503_three_times_gives_up_as_transient():
    route = respx.get(url__startswith="https://r.jina.ai/").mock(
        return_value=httpx.Response(503, json={"code": 503})
    )
    with pytest.raises(FetchError) as e:
        await JinaFetcher(gate()).get_html("https://acme.com/")
    assert e.value.error_class is ErrorClass.TRANSIENT
    assert route.call_count == 3          # 1 attempt + 2 retries


@respx.mock
async def test_jina_402_is_provider_account_and_not_retried():
    """Spec 17: a quota error pauses the job; it must never be retried or fail a domain."""
    route = respx.get(url__startswith="https://r.jina.ai/").mock(
        return_value=httpx.Response(402, json={"code": 402, "message": "quota"})
    )
    with pytest.raises(FetchError) as e:
        await JinaFetcher(gate()).get_html("https://acme.com/")
    assert e.value.error_class is ErrorClass.PROVIDER_ACCOUNT
    assert e.value.reason == "jina_account"
    assert route.call_count == 1


@respx.mock
async def test_jina_422_is_permanent_not_a_bug():
    """Verified against sqlite.org: unexpected content type. Retrying never helps."""
    route = respx.get(url__startswith="https://r.jina.ai/").mock(
        return_value=httpx.Response(422, json={"code": 422, "name": "AssertionFailureError"})
    )
    with pytest.raises(FetchError) as e:
        await JinaFetcher(gate()).get_html("https://sqlite.org/")
    assert e.value.error_class is ErrorClass.PERMANENT
    assert e.value.reason == "not_html"
    assert route.call_count == 1


@respx.mock
async def test_jina_429_penalises_bucket_and_honours_retry_after():
    g = FetchGate(rate_per_minute=600, global_concurrency=10, per_domain_concurrency=2)
    route = respx.get(url__startswith="https://r.jina.ai/")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}, json={"code": 429}),
        jina_ok(),
    ]
    r = await JinaFetcher(g).get_html("https://acme.com/")
    assert r.status == 200
    assert g.bucket._penalty_factor == pytest.approx(0.75)   # noqa: SLF001


@respx.mock
async def test_jina_200_with_error_payload_is_a_failed_fetch():
    """Spec 15: 200 carrying an error payload must be treated as a failure."""
    route = respx.get(url__startswith="https://r.jina.ai/").mock(
        return_value=httpx.Response(200, json={"code": 422, "data": None})
    )
    with pytest.raises(FetchError) as e:
        await JinaFetcher(gate()).get_html("https://acme.com/")
    assert e.value.error_class is ErrorClass.TRANSIENT
    assert route.call_count == 3


@respx.mock
async def test_bot_challenge_is_blocked_and_never_extracted():
    respx.get(url__startswith="https://r.jina.ai/").mock(
        return_value=jina_ok(html="<html><title>Just a moment...</title><body>cf-chl</body></html>")
    )
    with pytest.raises(FetchError) as e:
        await JinaFetcher(gate()).get_html("https://acme.com/")
    assert e.value.error_class is ErrorClass.BLOCKED
    assert e.value.reason == "blocked"


@respx.mock
async def test_site_404_through_jina_is_permanent():
    respx.get(url__startswith="https://r.jina.ai/").mock(
        return_value=jina_ok(site_status=404, html="<html>gone</html>")
    )
    with pytest.raises(FetchError) as e:
        await JinaFetcher(gate()).get_html("https://acme.com/")
    assert e.value.error_class is ErrorClass.PERMANENT


# --- httpx backend --------------------------------------------------------


@respx.mock
async def test_httpx_rejects_non_html_content_type():
    respx.get("https://acme.com/").mock(
        return_value=httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")
    )
    with pytest.raises(FetchError) as e:
        await HttpxFetcher(gate()).get_html("https://acme.com/")
    assert e.value.reason == "not_html"
    assert e.value.error_class is ErrorClass.PERMANENT


@respx.mock
async def test_httpx_truncates_at_max_page_bytes(monkeypatch):
    monkeypatch.setattr(F.settings, "max_page_bytes", 500)
    big = "<html><body>" + ("x" * 50_000) + "</body></html>"
    respx.get("https://acme.com/").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=big)
    )
    r = await HttpxFetcher(gate()).get_html("https://acme.com/")
    assert r.bytes <= 500
    assert len(r.html) <= 500


@respx.mock
async def test_httpx_redirect_loop_is_permanent():
    respx.get("https://acme.com/").mock(side_effect=httpx.TooManyRedirects("loop"))
    with pytest.raises(FetchError) as e:
        await HttpxFetcher(gate()).get_html("https://acme.com/")
    assert e.value.reason == "redirect_loop"
    assert e.value.error_class is ErrorClass.PERMANENT


@respx.mock
async def test_httpx_decodes_cp1252_when_utf8_fails():
    body = "caf\xe9 info@acme.com".encode("cp1252")
    respx.get("https://acme.com/").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, content=body)
    )
    r = await HttpxFetcher(gate()).get_html("https://acme.com/")
    assert "café" in r.html and "info@acme.com" in r.html


# --- pure classification --------------------------------------------------


def test_retry_after_parsing():
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("120") == 30.0            # capped
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0   # past date


@pytest.mark.parametrize(
    "status,expected",
    [(200, None), (401, ErrorClass.PROVIDER_ACCOUNT), (402, ErrorClass.PROVIDER_ACCOUNT),
     (422, ErrorClass.PERMANENT), (429, ErrorClass.TRANSIENT), (500, ErrorClass.TRANSIENT),
     (404, ErrorClass.PERMANENT)],
)
def test_provider_classification(status, expected):
    assert classify_provider_status(status) is expected


@pytest.mark.parametrize(
    "status,html,expected",
    [(200, "", None), (404, "", ErrorClass.PERMANENT), (500, "", ErrorClass.TRANSIENT),
     (429, "", ErrorClass.TRANSIENT), (403, "", ErrorClass.BLOCKED),
     (503, "just a moment", ErrorClass.BLOCKED), (503, "oops", ErrorClass.TRANSIENT)],
)
def test_site_classification(status, html, expected):
    assert classify_site_status(status, html) is expected


# --- the breaker is scoped to the PROVIDER, not to business sites ---------


@respx.mock
async def test_dead_business_sites_do_not_open_the_jina_breaker():
    """Observed on a real 42k roofer list: robots/sitemap failures on dead sites opened
    the Jina breaker within seconds and every remaining domain failed instantly."""
    g = gate()
    respx.get(url__startswith="https://").mock(side_effect=httpx.ConnectError("refused"))
    fetcher = HttpxFetcher(g)
    for i in range(40):
        with pytest.raises(FetchError):
            await fetcher.get_text(f"https://dead{i}.com/robots.txt")
    assert g.breaker.state == "closed"
    assert g.breaker.opened_count == 0


@respx.mock
async def test_site_level_404_through_jina_does_not_open_the_breaker():
    g = gate()
    respx.get(url__startswith="https://r.jina.ai/").mock(
        return_value=jina_ok(site_status=404, html="<html>gone</html>")
    )
    fetcher = JinaFetcher(g)
    for i in range(40):
        with pytest.raises(FetchError):
            await fetcher.get_html(f"https://gone{i}.com/")
    assert g.breaker.state == "closed"


@respx.mock
async def test_jina_5xx_does_open_the_breaker():
    g = gate()
    respx.get(url__startswith="https://r.jina.ai/").mock(
        return_value=httpx.Response(503, json={"code": 503})
    )
    fetcher = JinaFetcher(g)
    for i in range(15):
        with pytest.raises((FetchError, Exception)):
            await fetcher.get_html(f"https://site{i}.com/")
        if g.breaker.state != "closed":
            break
    assert g.breaker.state in ("open", "half_open")


@respx.mock
async def test_jina_403_is_a_blocked_site_not_an_account_error():
    """Verified live: r.jina.ai answers 403 when the target refuses it. Classifying that
    as a provider-account error paused the whole job on the first bot-protected site."""
    g = gate()
    respx.get(url__startswith="https://r.jina.ai/").mock(
        return_value=httpx.Response(403, json={"code": 403})
    )
    with pytest.raises(FetchError) as e:
        await JinaFetcher(g).get_html("https://blocked.com/")
    assert e.value.error_class is ErrorClass.BLOCKED
    assert e.value.reason == "blocked"
    assert g.breaker.opened_count == 0        # target sites never open the breaker


@respx.mock
async def test_409_token_budget_is_permanent_and_costs_nothing():
    """X-Token-Budget: Jina refuses an over-budget page with 409 and bills no tokens."""
    g = gate()
    route = respx.get(url__startswith="https://r.jina.ai/").mock(
        return_value=httpx.Response(409, json={"code": 409})
    )
    with pytest.raises(FetchError) as e:
        await JinaFetcher(g).get_html("https://huge.com/")
    assert e.value.error_class is ErrorClass.PERMANENT
    assert e.value.reason == "page_too_large"
    assert route.call_count == 1          # never retried
    assert g.breaker.opened_count == 0


def test_jina_headers_carry_target_selector_and_budget():
    from app.pipeline.fetch import JinaFetcher as J
    h = J(gate())._headers()
    assert "X-Target-Selector" in h
    for needed in ("footer", "mailto:", "data-cfemail", "application/ld+json", "tel:"):
        assert needed in h["X-Target-Selector"]
    assert h["X-Token-Budget"] == "50000"


@respx.mock
async def test_429_slows_the_bucket_but_never_opens_the_breaker():
    """Being rate-limited is us going too fast, not Jina being unhealthy. Treating it as
    provider failure opened the circuit, which then expired waiting domains as
    domain_timeout -- observed live as 138 circuit_open + 41 domain_timeout in 300 rows."""
    g = FetchGate(rate_per_minute=600, global_concurrency=10, per_domain_concurrency=2)
    respx.get(url__startswith="https://r.jina.ai/").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, json={"code": 429})
    )
    fetcher = JinaFetcher(g)
    for i in range(20):
        with pytest.raises(FetchError):
            await fetcher.get_html(f"https://site{i}.com/")
    assert g.breaker.state == "closed"
    assert g.breaker.opened_count == 0
    assert g.bucket._penalty_factor == pytest.approx(0.75)   # noqa: SLF001


def test_bucket_burst_is_small_enough_for_a_60s_window():
    from app.ratelimit import TokenBucket
    b = TokenBucket(rate_per_minute=500)
    assert b._capacity <= 10        # noqa: SLF001


@respx.mock
async def test_target_side_409_and_422_do_not_open_the_breaker():
    """409 is our own token budget refusing a page; 422 is the target serving a non-HTML
    content type. Marking them provider failures let target problems open the circuit."""
    for code in (409, 422):
        g = gate()
        respx.get(url__startswith="https://r.jina.ai/").mock(
            return_value=httpx.Response(code, json={"code": code})
        )
        fetcher = JinaFetcher(g)
        for i in range(40):
            with pytest.raises(FetchError):
                await fetcher.get_html(f"https://site{i}.com/")
        assert g.breaker.state == "closed", code
        assert g.breaker.opened_count == 0, code


@respx.mock
async def test_provider_timeouts_do_reach_the_breaker(monkeypatch):
    """_exc_to_fetch_error ignored its provider argument, so every timeout against
    r.jina.ai recorded as a SUCCESS."""
    from app.ratelimit import CircuitOpen, FetchGate as _FG
    monkeypatch.setattr(_FG._Slot, "MAX_BREAKER_WAIT_S", 0.05)
    g = gate()
    respx.get(url__startswith="https://r.jina.ai/").mock(
        side_effect=httpx.ConnectTimeout("slow")
    )
    fetcher = JinaFetcher(g)
    for i in range(30):
        with pytest.raises((FetchError, CircuitOpen)):
            await fetcher.get_html(f"https://site{i}.com/")
        if g.breaker.opened_count:
            break
    assert g.breaker.opened_count >= 1
    assert g.breaker.last_open_reasons, "the trip should record why it opened"


@respx.mock
async def test_breaker_records_why_it_opened(monkeypatch):
    from app.ratelimit import CircuitOpen, FetchGate as _FG
    monkeypatch.setattr(_FG._Slot, "MAX_BREAKER_WAIT_S", 0.05)
    g = gate()
    respx.get(url__startswith="https://r.jina.ai/").mock(
        return_value=httpx.Response(503, json={"code": 503})
    )
    fetcher = JinaFetcher(g)
    for i in range(30):
        with pytest.raises((FetchError, CircuitOpen)):
            await fetcher.get_html(f"https://s{i}.com/")
        if g.breaker.opened_count:
            break
    assert any("503" in k for k in g.breaker.last_open_reasons), g.breaker.last_open_reasons


# --- r.jina.ai is a PROXY: a slow target is not an unhealthy provider -----


@respx.mock
async def test_read_timeout_against_jina_is_not_blamed_on_the_provider():
    """Jina fetches the target for us, so a slow business site makes OUR request slow.
    Counting those opened the breaker on 3,095 domains of the 16k run, none of them
    Jina's fault -- the hand-built retry pass then found addresses on 54% of them."""
    g = gate()
    respx.get(url__startswith="https://r.jina.ai/").mock(
        side_effect=httpx.ReadTimeout("target is slow")
    )
    fetcher = JinaFetcher(g)
    for i in range(30):
        with pytest.raises(FetchError):
            await fetcher.get_html(f"https://slowsite{i}.com/")
    assert g.breaker.state == "closed"
    assert g.breaker.opened_count == 0


@respx.mock
async def test_a_read_timeout_is_still_transient_and_still_retried():
    """Not blaming the provider must not quietly turn it into a permanent failure."""
    g = gate()
    route = respx.get(url__startswith="https://r.jina.ai/")
    route.side_effect = [httpx.ReadTimeout("slow"), jina_ok()]
    r = await JinaFetcher(g).get_html("https://acme.com/")
    assert r.status == 200
    assert route.call_count == 2


@respx.mock
async def test_jina_deadline_expires_before_our_client_deadline():
    """X-Timeout was connect+read, i.e. 10s, while the client gave up at its 7s read
    timeout -- so Jina's own classified answer was never read on a slow page."""
    g = gate()
    route = respx.get(url__startswith="https://r.jina.ai/").mock(return_value=jina_ok())
    await JinaFetcher(g).get_html("https://acme.com/")
    request = route.calls[0].request
    assert int(request.headers["X-Timeout"]) < request.extensions["timeout"]["read"]


@respx.mock
async def test_direct_site_fetches_keep_the_tight_timeout():
    """The wider budget is per-request, so it must not leak onto target fetches."""
    g = gate()
    route = respx.get("https://acme.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *")
    )
    await HttpxFetcher(g).get_text("https://acme.com/robots.txt")
    assert route.calls[0].request.extensions["timeout"]["read"] == F.settings.read_timeout_s
