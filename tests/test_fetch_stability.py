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


# --- security review, 2026-11-13 -----------------------------------------------------


async def test_the_address_guard_refuses_when_its_own_lookup_fails(monkeypatch):
    async def boom(host, port):
        raise OSError("resolver dropped the query")

    monkeypatch.setattr(F.settings, "fetch_guard_private_addresses", True)
    monkeypatch.setattr(F.netguard, "resolve_public", boom)
    with pytest.raises(FetchError) as e:
        await F.guard_url("https://acme.com/")
    assert e.value.error_class is ErrorClass.TRANSIENT


def test_a_response_from_a_private_address_is_refused_before_its_body_is_read(monkeypatch):
    monkeypatch.setattr(F.settings, "fetch_guard_private_addresses", True)

    class Stream:
        def __init__(self, ip):
            self.ip = ip

        def get_extra_info(self, name):
            return (self.ip, 443) if name == "server_addr" else None

    def resp(ip):
        return httpx.Response(200, request=httpx.Request("GET", "https://acme.com/"),
                              extensions={"network_stream": Stream(ip)})

    F._check_peer(resp("93.184.216.34"))
    for ip in ("10.0.0.5", "127.0.0.1", "fd12::10", "169.254.169.254"):
        with pytest.raises(FetchError) as e:
            F._check_peer(resp(ip))
        assert e.value.reason == "private_address"


def test_a_compressed_body_is_inflated_to_the_page_cap_and_no_further():
    import gzip
    import zlib

    bomb = gzip.compress(b"A" * 50_000_000)
    assert len(bomb) < 100_000
    assert len(F._inflate(bomb, "gzip", 2_000_000)) == 2_000_000
    assert F._inflate(gzip.compress(b"<html>hi</html>"), "gzip", 1000) == b"<html>hi</html>"
    assert F._inflate(zlib.compress(b"<p>x</p>"), "deflate", 1000) == b"<p>x</p>"
    assert F._inflate(b"plain", "", 3) == b"pla"
    for enc in ("gzip, gzip", "br", "zstd"):
        with pytest.raises(FetchError):
            F._inflate(bomb, enc, 1000)
    with pytest.raises(FetchError):
        F._inflate(b"not gzip at all", "gzip", 1000)


@respx.mock
async def test_a_gzipped_site_is_still_read(no_guard):
    import gzip

    respx.get("https://acme.com/").mock(return_value=httpx.Response(
        200, content=gzip.compress(BIG.encode()),
        headers={"content-type": "text/html", "content-encoding": "gzip"}))
    r = await hybrid().get_html("https://acme.com/")
    assert "Acme Roofing" in r.html


def test_a_service_process_has_no_key_until_it_has_read_the_saved_ones(monkeypatch):
    monkeypatch.setattr(pk.settings, "jina_api_key", "env_key_0000")
    assert pk.current("jina") == "env_key_0000"          # a script: environment, as ever
    pk.set_process_name("worker")
    assert pk.current("jina") == "" and pk.source("jina") == "unreadable"
    pk._loaded_at = 1.0                                   # the first read succeeded
    assert pk.current("jina") == "env_key_0000"


def test_private_address_failures_are_not_retried():
    from app.queue import RETRYABLE_FAILURE_REASONS

    assert "private_address" not in RETRYABLE_FAILURE_REASONS


def test_tracebacks_are_redacted_too():
    import logging

    from app.logsetup import RedactingFormatter

    try:
        raise ValueError("Illegal header value b'Bearer jina_SUPERSECRETKEY1234567890'")
    except ValueError:
        import sys

        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "request failed", None,
                                sys.exc_info())
    out = RedactingFormatter(None).format(rec)
    assert "SUPERSECRET" not in out and "request failed" in out


# --- correctness review, 2026-11-13 --------------------------------------------------


@respx.mock
async def test_dead_keys_do_not_use_up_the_retries_while_a_good_key_remains():
    def answer(request):
        if request.headers["authorization"] == "Bearer key_good":
            return jina_ok()
        return httpx.Response(402)

    respx.get(url__startswith="https://r.jina.ai/").mock(side_effect=answer)
    _pool("key_dead1", "key_dead2", "key_dead3", "key_dead4", "key_good")
    r = await JinaFetcher(gate()).get_html("https://acme.com/")
    assert "contact" in r.html          # reached the good key inside one attempt
    assert "key_good" not in pk._benched


@respx.mock
async def test_an_open_jina_breaker_does_not_throw_away_a_page_fetched_directly(no_guard):
    from app.ratelimit import CircuitOpen

    respx.get("https://acme.com/").mock(return_value=httpx.Response(
        200, text=SHELL, headers={"content-type": "text/html"}))
    f = hybrid()

    async def refused(url, *, domain=None):
        raise CircuitOpen("jina", 30)

    f.render_html = refused
    r = await f.get_html("https://acme.com/")
    assert r.via == "direct"


async def test_the_second_pass_keeps_the_first_pass_and_survives_a_sick_provider(live_dns):
    from app.ratelimit import CircuitOpen

    form = BIG.replace("</body>", '<form action="/send"><input name="email"><textarea '
                       'name="message"></textarea></form><a href="tel:+13055550123">Call</a></body>')

    class Sick(_Renderer):
        async def render_html(self, url, *, domain=None):
            self.renders.append(url)
            raise CircuitOpen("jina", 30)

    sick = Sick(plain={"https://acme.com/": form}, rendered={})
    r = await run_mod.process_domain("acme.com", sick)
    assert r.error_reason is None and r.status != "fetch_failed" and sick.renders

    # A render that shows less than the first copy must not cost the form or phones.
    thin = _Renderer(plain={"https://acme.com/": form},
                     rendered={"https://acme.com/": "<html><body><footer>Acme</footer></body></html>"})
    r2 = await run_mod.process_domain("acme.com", thin)
    assert (r2.status, r2.phones, r2.contact_form_url) == (r.status, r.phones, r.contact_form_url)
    assert r2.pages_fetched == ["https://acme.com/"]


@respx.mock
async def test_a_rendered_fetch_reads_the_whole_page_once(no_guard):
    seen = []

    def jina(request):
        seen.append("x-target-selector" in request.headers)
        return httpx.Response(503)

    respx.get(url__startswith="https://r.jina.ai/").mock(side_effect=jina)
    with pytest.raises(FetchError):
        await JinaFetcher(gate()).get_html("https://acme.com/", render=True)
    assert seen == [False]                      # no region filter, and no retries


def test_pool_credit_merges_only_a_named_account_with_the_same_balance():
    import asyncio

    from app import providers

    async def run(wallets):
        _pool(*wallets)

        async def fake(client, key):
            return wallets[key]

        orig, providers.jina_wallet = providers.jina_wallet, fake
        try:
            return (await providers.jina_pool(None))["balance_tokens"]
        finally:
            providers.jina_wallet = orig

    # same account, same balance: one wallet
    assert asyncio.run(run({"k_aaaa": (300, "acct"), "k_bbbb": (300, "acct")})) == 300
    # no account id: never merged (two new keys both start on the same free credit)
    assert asyncio.run(run({"k_aaaa": (10, None), "k_bbbb": (10, None)})) == 20
    # same account id, different balances: per-key wallets, both count
    assert asyncio.run(run({"k_aaaa": (300, "acct"), "k_bbbb": (50, "acct")})) == 350


def test_a_first_successful_key_read_counts_as_a_change_so_the_worker_retries():
    pk.set_process_name("worker")
    before = pk.signature("jina")
    pk._loaded_at = 1.0
    assert pk.signature("jina") != before
