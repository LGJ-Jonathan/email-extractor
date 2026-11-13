"""The 18k Miami run (2026-11-13): 64% of finished domains were fetch_failed, and all
but ~400 of the 3,610 were ours -- 1,760 circuit_open from a full connection pool
booked as Jina being down, 1,446 dns from lookups that failed without an answer."""

import dns.exception
import dns.resolver
import httpx
import pytest
import respx

from app import provider_keys as pk
from app.pipeline import dns_check
from app.pipeline import fetch as F
from app.pipeline import run as run_mod
from app.pipeline.dns_check import DnsResult
from app.pipeline.fetch import ErrorClass, FetchError, JinaFetcher
from app.ratelimit import FetchGate, TokenBucket
from tests.test_fetch import PAGE, jina_ok


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    await F.close_client()
    pk.reset_for_tests()
    monkeypatch.setattr(F._BaseFetcher, "RETRY_DELAYS", (0.001, 0.002))
    yield
    await F.close_client()
    pk.reset_for_tests()


def gate() -> FetchGate:
    return FetchGate(rate_per_minute=None, global_concurrency=10, per_domain_concurrency=2,
                     provider="jina")


# --- the connection pool -----------------------------------------------------------


def test_provider_and_direct_fetches_do_not_share_a_pool():
    assert F.get_provider_client() is not F.get_client()
    assert F.get_provider_client().timeout.pool == F.POOL_WAIT_S
    assert F.get_client().timeout.pool == F.POOL_WAIT_S


@respx.mock
async def test_a_full_connection_pool_never_opens_the_jina_breaker():
    respx.get(url__startswith="https://r.jina.ai/").mock(side_effect=httpx.PoolTimeout("full"))
    g = gate()
    f = JinaFetcher(g)
    for i in range(40):
        with pytest.raises(FetchError) as e:
            await f.get_html(f"https://site{i}.com/")
        assert e.value.error_class is ErrorClass.TRANSIENT and not e.value.provider
    assert g.breaker.state == "closed" and g.breaker.opened_count == 0


@respx.mock
async def test_jina_refusing_connections_still_opens_the_breaker():
    respx.get(url__startswith="https://r.jina.ai/").mock(side_effect=httpx.ConnectTimeout("down"))
    g = gate()
    f = JinaFetcher(g)
    for i in range(30):
        try:
            await f.get_html(f"https://site{i}.com/")
        except Exception:  # noqa: BLE001
            pass
        if g.breaker.state != "closed":
            break
    assert g.breaker.opened_count >= 1


# --- the key pool -------------------------------------------------------------------


def _pool(*keys: str) -> None:
    pk._keys["jina"] = [pk.Entry(str(i), k, k[-4:], None, None) for i, k in enumerate(keys)]


@respx.mock
async def test_requests_rotate_across_the_pool():
    seen = []

    def answer(request):
        seen.append(request.headers["authorization"])
        return jina_ok()

    respx.get(url__startswith="https://r.jina.ai/").mock(side_effect=answer)
    _pool("key_aaaa", "key_bbbb")
    f = JinaFetcher(gate())
    for i in range(4):
        await f.get_html(f"https://site{i}.com/")
    assert seen == ["Bearer key_aaaa", "Bearer key_bbbb"] * 2


@respx.mock
async def test_a_key_out_of_credit_is_benched_and_the_next_one_finishes_the_request():
    def answer(request):
        if request.headers["authorization"] == "Bearer key_aaaa":
            return httpx.Response(402, json={"code": 402})
        return jina_ok()

    respx.get(url__startswith="https://r.jina.ai/").mock(side_effect=answer)
    _pool("key_aaaa", "key_bbbb")
    g = gate()
    r = await JinaFetcher(g).get_html("https://acme.com/")
    assert "contact" in r.html
    assert pk.benched_report() == {"0": "empty"}
    assert pk.usable_count("jina") == 1
    assert g.breaker.opened_count == 0


@respx.mock
async def test_the_job_still_pauses_when_every_key_is_spent():
    respx.get(url__startswith="https://r.jina.ai/").mock(return_value=httpx.Response(402))
    _pool("key_aaaa", "key_bbbb")
    with pytest.raises(FetchError) as e:
        await JinaFetcher(gate()).get_html("https://acme.com/")
    assert e.value.error_class is ErrorClass.PROVIDER_ACCOUNT
    assert pk.benched_report() == {"0": "empty", "1": "empty"}


@respx.mock
async def test_a_benched_key_returns_once_jina_accepts_it_again():
    respx.get(url__startswith="https://r.jina.ai/").mock(return_value=jina_ok(PAGE))
    _pool("key_aaaa")
    pk._benched["key_aaaa"] = (10**12, "empty")
    await JinaFetcher(gate()).get_html("https://acme.com/")
    assert pk.benched_report() == {}


def test_the_rate_budget_follows_the_number_of_usable_keys():
    n = {"keys": 1}
    b = TokenBucket(600, capacity=5, scale=lambda: n["keys"])
    assert b._effective_rate() == pytest.approx(10.0)
    n["keys"] = 3
    assert b._effective_rate() == pytest.approx(30.0)
    b.penalize(0.5, 60)
    assert b._effective_rate() == pytest.approx(15.0)


# --- DNS ----------------------------------------------------------------------------


class _Resolver:
    """Answers from a table: {(name, rdtype): [values] | Exception}."""

    def __init__(self, table, default):
        self.table, self.default, self.asked = table, default, 0

    async def resolve(self, name, rdtype):
        self.asked += 1
        v = self.table.get((name, rdtype), self.default)
        if isinstance(v, Exception):
            raise v
        return v


def _install(monkeypatch, system, fallback):
    monkeypatch.setattr(dns_check, "_resolver",
                        lambda nameservers=None: fallback if nameservers else system)


async def test_a_lookup_that_times_out_is_asked_again_of_public_resolvers(monkeypatch):
    system = _Resolver({}, dns.exception.Timeout())
    fallback = _Resolver({("acme.com", "A"): ["1.2.3.4"]}, dns.resolver.NoAnswer())
    _install(monkeypatch, system, fallback)
    r = await dns_check.check_domain("acme.com")
    assert r.resolves and r.a_domain and not r.transient
    assert fallback.asked > 0


async def test_a_lookup_that_never_answers_is_transient_not_a_dead_domain(monkeypatch):
    _install(monkeypatch, _Resolver({}, dns.exception.Timeout()),
             _Resolver({}, dns.resolver.NoNameservers()))
    r = await dns_check.check_domain("acme.com")
    assert not r.resolves and r.transient and not r.nxdomain
    assert r.error_class == "transient"


async def test_nxdomain_is_believed_without_asking_twice(monkeypatch):
    fallback = _Resolver({}, dns.resolver.NXDOMAIN())
    _install(monkeypatch, _Resolver({}, dns.resolver.NXDOMAIN()), fallback)
    r = await dns_check.check_domain("gone.com")
    assert not r.resolves and r.nxdomain and not r.transient
    assert r.error_class == "permanent" and fallback.asked == 0


async def test_a_failed_lookup_is_retried_and_reported_as_dns_timeout(monkeypatch):
    calls = []

    async def check(domain):
        calls.append(domain)
        return DnsResult(resolves=False, transient=True, error="Timeout")

    monkeypatch.setattr(run_mod, "check_domain", check)
    monkeypatch.setattr(run_mod, "MIN_RETRY_DELAY_S", 0.001)
    monkeypatch.setattr(run_mod.settings, "max_domain_attempts", 3)
    r = await run_mod.process_domain("acme.com", object())
    assert (r.status, r.error_reason) == ("fetch_failed", "dns_timeout")
    assert len(calls) == 3
    from app.queue import RETRYABLE_FAILURE_REASONS

    assert "dns_timeout" in RETRYABLE_FAILURE_REASONS


# --- hybrid: our own fetch first, Jina for what it could not get --------------------

BIG = "<html><body><h1>Acme Roofing</h1>" + "<p>We fix roofs across the county.</p>" * 20 \
      + '<a href="/contact">Contact</a></body></html>'
SHELL = '<html><body><div id="root"></div></body></html>'


def hybrid():
    from app.pipeline.fetch import HybridFetcher

    direct = FetchGate(rate_per_minute=None, global_concurrency=10, per_domain_concurrency=2,
                       provider="direct")
    return HybridFetcher(gate(), direct)


@pytest.fixture
def no_guard(monkeypatch):
    monkeypatch.setattr(F.settings, "fetch_guard_private_addresses", False)


@respx.mock
async def test_hybrid_fetches_a_plain_site_itself_and_never_calls_jina(no_guard):
    respx.get("https://acme.com/").mock(return_value=httpx.Response(
        200, text=BIG, headers={"content-type": "text/html"}))
    jina = respx.get(url__startswith="https://r.jina.ai/").mock(return_value=jina_ok())
    f = hybrid()
    r = await f.get_html("https://acme.com/")
    assert r.via == "direct" and r.jina_tokens is None and "Acme Roofing" in r.html
    assert not jina.called and f.stats["direct"] == 1


@respx.mock
@pytest.mark.parametrize("answer", [httpx.Response(403, text="Forbidden"),
                                    httpx.ConnectTimeout("slow"),
                                    httpx.Response(503, text="busy")])
async def test_hybrid_sends_a_site_that_refuses_us_to_jina(no_guard, answer):
    kw = {"side_effect": answer} if isinstance(answer, Exception) else {"return_value": answer}
    respx.get("https://acme.com/").mock(**kw)
    seen = []

    def jina(request):
        seen.append(request.headers.get("x-engine"))
        return jina_ok(BIG)

    respx.get(url__startswith="https://r.jina.ai/").mock(side_effect=jina)
    r = await hybrid().get_html("https://acme.com/")
    assert r.via == "jina" and seen == ["direct"]


@respx.mock
async def test_hybrid_does_not_pay_jina_for_a_page_that_is_simply_not_there(no_guard):
    respx.get("https://acme.com/gone").mock(return_value=httpx.Response(404, text="nope"))
    jina = respx.get(url__startswith="https://r.jina.ai/").mock(return_value=jina_ok())
    with pytest.raises(FetchError):
        await hybrid().get_html("https://acme.com/gone")
    assert not jina.called


@respx.mock
async def test_hybrid_renders_a_javascript_shell_in_jinas_browser(no_guard):
    respx.get("https://acme.com/").mock(return_value=httpx.Response(
        200, text=SHELL, headers={"content-type": "text/html"}))
    seen = []

    def jina(request):
        seen.append(("x-engine" in request.headers, request.headers["x-timeout"]))
        return jina_ok(BIG)

    respx.get(url__startswith="https://r.jina.ai/").mock(side_effect=jina)
    r = await hybrid().get_html("https://acme.com/")
    # No X-Engine header at all: Jina's default engine is the headless browser.
    assert r.via == "jina_render" and seen == [(False, str(F.settings.jina_render_timeout_s))]


@respx.mock
async def test_hybrid_keeps_the_shell_when_rendering_fails(no_guard):
    respx.get("https://acme.com/").mock(return_value=httpx.Response(
        200, text=SHELL, headers={"content-type": "text/html"}))
    respx.get(url__startswith="https://r.jina.ai/").mock(return_value=httpx.Response(422))
    r = await hybrid().get_html("https://acme.com/")
    assert r.via == "direct"


class _Renderer:
    """A fetcher whose plain pages hide the address that the rendered ones show."""

    def __init__(self, plain: dict, rendered: dict):
        self.plain, self.rendered, self.renders = plain, rendered, []

    async def get_html(self, url, *, domain=None):
        if url not in self.plain:
            raise FetchError(ErrorClass.PERMANENT, "homepage", "HTTP 404", 404)
        return F.FetchResult(url=url, final_url=url, status=200, html=self.plain[url], bytes=1)

    async def render_html(self, url, *, domain=None):
        self.renders.append(url)
        if url not in self.rendered:
            raise FetchError(ErrorClass.PERMANENT, "homepage", "jina HTTP 422", 422)
        return F.FetchResult(url=url, final_url=url, status=200, html=self.rendered[url],
                             bytes=1, jina_tokens=700, via="jina_render")

    async def get_text(self, url, *, domain=None):
        raise FetchError(ErrorClass.PERMANENT, "not_html", "HTTP 404", 404)


@pytest.fixture
def live_dns(monkeypatch):
    async def check(domain):
        return DnsResult(resolves=True, a_domain=True, mx_provider="google",
                         ns_hosts=["ns1.cloudflare.com"])

    monkeypatch.setattr(run_mod, "check_domain", check)
    monkeypatch.setattr(run_mod._judge, "judge",
                        lambda ranked, domain, title="": _rules(ranked))


async def _rules(ranked):
    from app.pipeline.typesafe import rules_only

    return rules_only(ranked)


async def test_a_domain_with_no_address_gets_a_second_pass_in_the_browser(live_dns):
    f = _Renderer(
        plain={"https://acme.com/": BIG},
        rendered={"https://acme.com/": BIG.replace(
            "</body>", '<a href="mailto:office@acme.com">Email us</a></body>')},
    )
    r = await run_mod.process_domain("acme.com", f)
    assert r.status == "found" and r.best_email == "office@acme.com"
    assert f.renders == ["https://acme.com/"] and r.jina_tokens == 700


async def test_no_second_pass_when_the_first_one_found_an_address(live_dns):
    f = _Renderer(plain={"https://acme.com/": BIG.replace(
        "</body>", '<a href="mailto:hi@acme.com">Email</a></body>')}, rendered={})
    r = await run_mod.process_domain("acme.com", f)
    assert r.best_email == "hi@acme.com" and f.renders == [] and r.jina_tokens == 0


async def test_the_second_pass_can_be_switched_off(live_dns, monkeypatch):
    monkeypatch.setattr(run_mod.settings, "hybrid_second_pass", False)
    f = _Renderer(plain={"https://acme.com/": BIG}, rendered={})
    r = await run_mod.process_domain("acme.com", f)
    assert f.renders == [] and r.status != "found"
