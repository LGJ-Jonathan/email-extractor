"""Stage 1 DNS classification, stage 2 homepage ladder, and parked detection."""

import pytest

from app.pipeline import run as run_mod
from app.pipeline.dns_check import DnsResult, classify_mx
from app.pipeline.fetch import ErrorClass, FetchError, FetchResult
from app.pipeline.parked import check_final_url, check_ns, check_text, is_parked, visible_text
from app.pipeline.run import fetch_homepage, process_domain
from app.pipeline.status import StatusInputs, assign_status


# --- MX classification ----------------------------------------------------


@pytest.mark.parametrize(
    "hosts,expected",
    [
        ([], "none"),
        (["aspmx.l.google.com"], "google"),
        (["alt1.aspmx.l.googlemail.com"], "google"),
        (["acme-com.mail.protection.outlook.com"], "microsoft"),
        (["acme.mail.outlook.com"], "microsoft"),
        (["mx.zoho.com"], "other"),
        (["mx.zoho.com", "aspmx.l.google.com"], "google"),   # google wins
    ],
)
def test_classify_mx(hosts, expected):
    assert classify_mx(hosts) == expected


def test_dns_error_class():
    assert DnsResult(resolves=True).error_class is None
    assert DnsResult(resolves=False, nxdomain=True).error_class == "permanent"
    assert DnsResult(resolves=False, nxdomain=False).error_class == "transient"


# --- parked detection -----------------------------------------------------


@pytest.mark.parametrize(
    "ns",
    [["ns1.sedoparking.com"], ["ns1.bodis.com"], ["a.parkingcrew.net"],
     ["ns1.above.com"], ["ns.dan.com"], ["dns1.afternic.com"],
     ["ns1.hugedomains.com"], ["ns1.namebrightdns.com"], ["ns.parklogic.com"]],
)
def test_parking_nameservers(ns):
    assert check_ns(ns).parked


def test_normal_nameservers_are_not_parked():
    assert not check_ns(["ns1.cloudflare.com", "ns2.godaddy.com"]).parked


@pytest.mark.parametrize(
    "url",
    ["https://dan.com/buy-domain/acme.com", "https://www.afternic.com/domain/acme.com",
     "https://hugedomains.com/domain_profile.cfm?d=acme", "https://sedo.com/search/",
     "https://www.godaddy.com/forsale/acme.com", "https://undeveloped.com/acme"],
)
def test_parking_hosts(url):
    assert check_final_url(url).parked


def test_godaddy_outside_forsale_is_not_parked():
    assert not check_final_url("https://www.godaddy.com/help").parked


@pytest.mark.parametrize(
    "phrase",
    ["This domain is for sale", "Buy this domain", "domain may be for sale",
     "This domain has been registered"],
)
def test_explicit_for_sale_phrases_win_at_any_length(phrase):
    """Resolved ambiguity: an explicit for-sale page is parked even when it is wordy."""
    long_page = f"<html><body><p>{phrase}.</p><p>{'filler text. ' * 60}</p></body></html>"
    assert len(visible_text(long_page)) > 300
    assert check_text(long_page).parked


def test_weak_phrase_needs_the_length_guard():
    short = "<html><body>Coming soon</body></html>"
    long = "<html><body>Coming soon to our second location! " + ("Real content. " * 40) + "</body></html>"
    assert check_text(short).parked
    assert len(visible_text(long)) > 300
    assert not check_text(long).parked


def test_real_business_page_is_not_parked():
    html = "<html><body><h1>Acme Roofing</h1><p>Call 214-555-0143 for a free estimate.</p></body></html>"
    assert not is_parked(ns_hosts=["ns1.cloudflare.com"], final_url="https://acme.com/", html=html)


def test_visible_text_drops_scripts_and_styles():
    html = "<html><head><style>b{color:red}</style><script>var x='for sale'</script></head>" \
           "<body><p>Acme Roofing</p></body></html>"
    t = visible_text(html)
    assert "Acme Roofing" in t and "for sale" not in t and "color:red" not in t


# --- stage 2 homepage ladder ---------------------------------------------


class FakeFetcher:
    def __init__(self, behaviour: dict[str, object]):
        self.behaviour = behaviour
        self.calls: list[str] = []

    async def get_html(self, url: str, *, domain: str | None = None) -> FetchResult:
        self.calls.append(url)
        outcome = self.behaviour.get(url, FetchError(ErrorClass.TRANSIENT, "homepage"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def get_text(self, url: str, *, domain: str | None = None) -> FetchResult:
        raise NotImplementedError


def ok(url: str) -> FetchResult:
    return FetchResult(url=url, final_url=url, status=200, html="<html><a href='/x'>x</a></html>", bytes=40)


async def test_ladder_stops_at_the_first_success():
    f = FakeFetcher({"https://acme.com/": ok("https://acme.com/")})
    res, err = await fetch_homepage(f, "acme.com")
    assert res is not None and err is None
    assert f.calls == ["https://acme.com/"]          # spec 16: never fetch www too


async def test_ladder_falls_through_to_www_then_http():
    f = FakeFetcher({"http://acme.com/": ok("http://acme.com/")})
    res, err = await fetch_homepage(f, "acme.com")
    assert res is not None
    assert f.calls == ["https://acme.com/", "https://www.acme.com/", "http://acme.com/"]


async def test_ladder_exhausted_returns_the_last_error():
    f = FakeFetcher({})
    res, err = await fetch_homepage(f, "acme.com")
    assert res is None and err is not None
    assert len(f.calls) == 3


async def test_provider_account_error_aborts_the_ladder_immediately():
    """A quota error is not this domain's fault; burning the other rungs wastes calls."""
    f = FakeFetcher({"https://acme.com/": FetchError(ErrorClass.PROVIDER_ACCOUNT, "jina_account")})
    res, err = await fetch_homepage(f, "acme.com")
    assert res is None
    assert err.error_class is ErrorClass.PROVIDER_ACCOUNT
    assert f.calls == ["https://acme.com/"]


# --- process_domain -------------------------------------------------------


@pytest.fixture
def fake_dns(monkeypatch):
    def install(**kw):
        async def _check(domain: str) -> DnsResult:
            return DnsResult(**{"resolves": True, "mx_provider": "google", **kw})
        monkeypatch.setattr(run_mod, "check_domain", _check)
    return install


async def test_dead_dns_is_fetch_failed(fake_dns):
    fake_dns(resolves=False, mx_provider="none")
    r = await process_domain("acme.com", FakeFetcher({}))
    assert (r.status, r.error_reason) == ("fetch_failed", "dns")


async def test_parking_nameservers_skip_the_fetch_entirely(fake_dns):
    fake_dns(ns_hosts=["ns1.sedoparking.com"])
    f = FakeFetcher({})
    r = await process_domain("acme.com", f)
    assert r.status == "parked_or_for_sale"
    assert f.calls == []                      # spec: parked domains skip stages 3-7


async def test_for_sale_homepage_is_parked(fake_dns):
    fake_dns(ns_hosts=["ns1.cloudflare.com"])
    page = FetchResult(
        url="https://acme.com/", final_url="https://acme.com/", status=200,
        html="<html><body>This domain is for sale</body></html>", bytes=50,
    )
    r = await process_domain("acme.com", FakeFetcher({"https://acme.com/": page}))
    assert r.status == "parked_or_for_sale"


async def test_healthy_domain_runs_the_full_built_pipeline(fake_dns):
    fake_dns(ns_hosts=["ns1.cloudflare.com"], mx_provider="microsoft")
    page = FetchResult(
        url="https://acme.com/", final_url="https://acme.com/", status=200,
        html='<html><body><a href="mailto:mike@acme.com">Email</a>'
             '<a href="tel:+12123794444">Call</a>'
             '<a href="https://facebook.com/acme">fb</a></body></html>',
        bytes=90,
    )
    r = await process_domain("acme.com", FakeFetcher({"https://acme.com/": page}))
    assert r.mx_provider == "microsoft"
    assert r.final_domain == "acme.com"
    assert r.status == "found"
    assert r.best_email == "mike@acme.com"
    assert r.best_email_source == "rules"
    assert r.business_emails == ["mike@acme.com"]
    assert r.all_emails[0].method == "mailto" and r.all_emails[0].rank == 1
    assert r.phones == ["+12123794444"]
    assert r.socials["facebook"] == "https://facebook.com/acme"
    assert r.error_reason is None


async def test_redirect_to_another_domain_records_final_domain(fake_dns):
    fake_dns(ns_hosts=["ns1.cloudflare.com"])
    page = FetchResult(
        url="https://oldacme.com/", final_url="https://www.newacme.com/home",
        status=200, html="<html>hi</html>", bytes=20,
    )
    r = await process_domain("oldacme.com", FakeFetcher({"https://oldacme.com/": page}))
    assert r.domain == "oldacme.com"
    assert r.final_domain == "newacme.com"


async def test_unexpected_exception_becomes_internal_error(fake_dns, monkeypatch):
    """Spec 17 Bug class: a crash must never escape process_domain."""
    fake_dns(ns_hosts=["ns1.cloudflare.com"])

    class Boom:
        async def get_html(self, url, *, domain=None):
            raise ValueError("kaboom")
        async def get_text(self, url, *, domain=None):
            raise NotImplementedError

    r = await process_domain("acme.com", Boom())
    assert (r.status, r.error_reason) == ("fetch_failed", "internal_error")


# --- section 7 table ------------------------------------------------------


def test_status_table_order():
    assert assign_status(StatusInputs(invalid_input=True, best_email="a@b.com")) == "invalid_input"
    assert assign_status(StatusInputs(fetch_failed=True, best_email="a@b.com")) == "fetch_failed"
    assert assign_status(StatusInputs(parked=True)) == "parked_or_for_sale"
    assert assign_status(StatusInputs(typesafe_said_none=True)) == "no_business_email"
    assert assign_status(StatusInputs(best_email="a@b.com")) == "found"
    assert assign_status(StatusInputs(contact_form_url="https://x/c")) == "form_only"
    assert assign_status(StatusInputs()) == "no_contact_info"


def test_a_real_address_is_never_buried_under_a_parked_verdict():
    """Spec order put parked above found, so TypeSafe calling a live site "parked" while
    we held a valid own-domain email shipped that email under a status every downstream
    filter discards. Deterministic parking still short-circuits before extraction."""
    assert assign_status(StatusInputs(parked=True, best_email="mike@acme.com")) == "found"


def test_no_business_email_never_contradicts_business_emails():
    s = StatusInputs(typesafe_said_none=True, business_emails=["careers@acme.com"])
    assert assign_status(s) != "no_business_email"


async def test_early_stop_is_off_so_every_page_is_still_fetched(fake_dns, monkeypatch):
    """With COLLECT_ALL_EMAILS the goal is every address on the site, so stopping at the
    first good one silently drops the addresses on the pages we skip."""
    from app.settings import settings as st
    monkeypatch.setattr(st, "early_stop_on_first_good", False)
    fake_dns(ns_hosts=["ns1.cloudflare.com"])
    home = FetchResult(
        url="https://acme.com/", final_url="https://acme.com/", status=200,
        html='<html><body><a href="mailto:mike@acme.com">Mike</a>'
             '<a href="/contact">Contact</a><a href="/about-us">About</a></body></html>',
        bytes=80,
    )
    contact = FetchResult(
        url="https://acme.com/contact", final_url="https://acme.com/contact", status=200,
        html='<html><body><a href="mailto:sales@acme.com">Sales</a></body></html>', bytes=60,
    )
    about = FetchResult(
        url="https://acme.com/about-us", final_url="https://acme.com/about-us", status=200,
        html='<html><body><a href="mailto:owner@acme.com">Owner</a></body></html>', bytes=60,
    )
    f = FakeFetcher({
        "https://acme.com/": home,
        "https://acme.com/contact": contact,
        "https://acme.com/about-us": about,
    })
    r = await process_domain("acme.com", f)
    got = {e.email for e in r.all_emails}
    assert got == {"mike@acme.com", "sales@acme.com", "owner@acme.com"}


async def test_timeout_keeps_the_pages_already_fetched(fake_dns, monkeypatch):
    """Spec 16.5: on expiry, finish with whatever was extracted. It was discarding
    everything, throwing away 288 of 845 real domains AFTER paying to fetch them."""
    import asyncio as _aio
    from app.settings import settings as st
    monkeypatch.setattr(st, "domain_timeout_s", 1)
    fake_dns(ns_hosts=["ns1.cloudflare.com"])

    home = FetchResult(
        url="https://acme.com/", final_url="https://acme.com/", status=200,
        html='<html><body><a href="mailto:mike@acme.com">Mike</a>'
             '<a href="/contact">Contact</a></body></html>', bytes=70,
    )

    class SlowAfterHomepage:
        def __init__(self):
            self.calls = []

        async def get_html(self, url, *, domain=None):
            self.calls.append(url)
            if url == "https://acme.com/":
                return home
            await _aio.sleep(30)          # never returns before the deadline
            raise AssertionError("unreachable")

        async def get_text(self, url, *, domain=None):
            raise RuntimeError("no robots")

    r = await process_domain("acme.com", SlowAfterHomepage())
    assert r.error_reason == "domain_timeout"
    assert r.best_email == "mike@acme.com"        # salvaged, not discarded
    assert r.status == "found"
    assert r.needs_review is True                 # flagged as partial
    assert r.pages_fetched == ["https://acme.com/"]


async def test_timeout_with_nothing_fetched_is_still_a_clean_failure(fake_dns, monkeypatch):
    import asyncio as _aio
    from app.settings import settings as st
    monkeypatch.setattr(st, "domain_timeout_s", 1)
    fake_dns(ns_hosts=["ns1.cloudflare.com"], mx_provider="google")

    class NeverReturns:
        async def get_html(self, url, *, domain=None):
            await _aio.sleep(30)
        async def get_text(self, url, *, domain=None):
            await _aio.sleep(30)

    r = await process_domain("acme.com", NeverReturns())
    assert (r.status, r.error_reason) == ("fetch_failed", "domain_timeout")
    assert r.mx_provider == "google"               # DNS work still reported
