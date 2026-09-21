"""Pipeline orchestrator (spec section 6).

Built so far: stage 1 (normalise + DNS), stage 2 (homepage ladder), parked detection,
and section 16's per-domain hard deadline. Stages 3-7 arrive in steps 4-7; until then a
domain that gets past stage 2 finishes with error_reason="stages_3_7_not_built" so a
partial result can never be mistaken for a real one.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.pipeline import parked as parked_mod
from app.pipeline.parked import visible_text
from app.pipeline.discovery import discover_pages, fetch_text_or_blank
from app.pipeline.dns_check import DnsResult, check_domain
from app.pipeline.fetch import Fetcher, FetchError, FetchResult, build_fetcher
from app.ratelimit import CircuitOpen
from app.pipeline.normalize import crawl_url, host_of_key, normalize_input, registrable
from app.pipeline.extract.emails import EmailCandidate, extract_page, merge_pages
from app.pipeline.extract.forms import pick_contact_form_url
from app.pipeline.extract.phones import extract_phones
from app.pipeline.extract.socials import extract_socials
from app.pipeline.filter_rank import rank_candidates, rules_best
from app.pipeline.status import StatusInputs, assign_status
from app.pipeline.typesafe import TypeSafeClient, rules_only
from app.schemas import DomainResult, EmailCandidateOut
from app.settings import settings

log = logging.getLogger("email_extractor.pipeline")

# One client per process: it owns the TypeSafe concurrency limit and its own breaker.
_judge = TypeSafeClient()


@dataclass
class Scratch:
    """What a domain has accumulated so far.

    Spec 16.5 says a domain that hits its deadline should "finish with whatever was
    extracted so far". It was instead discarding everything, which on the real list threw
    away 288 of 845 domains AFTER paying to fetch their pages.
    """

    dns: DnsResult | None = None
    final_domain: str | None = None
    home_html: str = ""
    pages: list[tuple[str, int, str]] = field(default_factory=list)
    tokens: int = 0

# Spec stage 2. A fourth rung, http://www.{d}/, would also cover www-only legacy sites
# that serve no TLS; it is deliberately not added because the spec lists three.
HOMEPAGE_LADDER = (
    ("https", False),
    ("https", True),
    ("http", False),
)


async def fetch_homepage(
    fetcher: Fetcher, domain: str
) -> tuple[FetchResult | None, FetchError | None]:
    """Try each rung in order; the first success wins (spec 16: never fetch both)."""
    last: FetchError | None = None
    for scheme, www in HOMEPAGE_LADDER:
        url = crawl_url(domain, scheme=scheme, www=www)
        try:
            return await fetcher.get_html(url, domain=domain), None
        except FetchError as e:
            last = e
            # A provider account error is not this domain's fault - stop immediately
            # so the job can pause rather than burning the remaining rungs.
            if e.error_class.value == "provider_account":
                return None, e
    return None, last


def _result(domain: str, **kw) -> DomainResult:
    return DomainResult(domain=domain, **kw)


async def process_domain(domain: str, fetcher: Fetcher | None = None) -> DomainResult:
    """Run the stages for one crawl key. Never raises for a domain-level problem."""
    started = time.perf_counter()
    fetcher = fetcher or build_fetcher()
    scratch = Scratch()
    try:
        async with asyncio.timeout(settings.domain_timeout_s):
            return await _process(domain, fetcher, started, scratch)
    except CircuitOpen:
        # Spec 17 level 3: the provider is unhealthy, so this is not the domain's fault.
        # Transient, and the retry pass picks it up.
        log.warning("circuit_open", extra={"domain": domain})
        return _result(domain, status="fetch_failed", error_reason="circuit_open")
    except TimeoutError:
        log.warning(
            "domain_timeout",
            extra={"domain": domain, "pages_salvaged": len(scratch.pages)},
        )
        if scratch.pages:
            # Spec 16.5: keep what was already fetched. TypeSafe is skipped so the
            # salvage path cannot itself block.
            return _harvest(
                domain, scratch, judgment=None, started=started,
                error_reason="domain_timeout",
            )
        return _result(
            domain, status="fetch_failed", error_reason="domain_timeout",
            mx_provider=scratch.dns.mx_provider if scratch.dns else None,
        )
    except Exception as e:  # noqa: BLE001 - spec 17 Bug class: never fail the job
        log.exception("internal_error", extra={"domain": domain})
        return _result(
            domain,
            status="fetch_failed",
            error_reason="internal_error",
            site_status=f"{type(e).__name__}",
        )


async def _process(
    domain: str, fetcher: Fetcher, started: float, scratch: Scratch
) -> DomainResult:
    # --- stage 1: DNS
    dns: DnsResult = await check_domain(domain)
    scratch.dns = dns
    if not dns.resolves:
        return _result(
            domain,
            status="fetch_failed",
            error_reason="dns",
            mx_provider=dns.mx_provider,
        )

    # Parking nameservers settle it without a fetch.
    verdict = parked_mod.check_ns(dns.ns_hosts)
    if verdict.parked:
        return _result(
            domain, status="parked_or_for_sale", mx_provider=dns.mx_provider,
            site_status="parked_or_for_sale",
        )

    # --- stage 2 + wave 1 (spec 16): homepage, robots.txt and /sitemap.xml together
    host = host_of_key(domain)
    home_r, robots_text, sitemap_text = await asyncio.gather(
        fetch_homepage(fetcher, domain),
        fetch_text_or_blank(fetcher, f"https://{host}/robots.txt", domain),
        fetch_text_or_blank(fetcher, f"https://{host}/sitemap.xml", domain),
    )
    home, err = home_r
    if home is None:
        reason = err.reason if err else "homepage"
        if err and err.error_class.value == "provider_account":
            raise err            # bubbles to the worker, which pauses the job (spec 17)
        return _result(
            domain, status="fetch_failed", error_reason=reason, mx_provider=dns.mx_provider
        )

    final_domain = registrable(_host(home.final_url)) or domain
    scratch.final_domain = final_domain
    scratch.home_html = home.html

    verdict = parked_mod.is_parked(final_url=home.final_url, html=home.html)
    if verdict.parked:
        return _result(
            domain,
            final_domain=final_domain,
            status="parked_or_for_sale",
            site_status="parked_or_for_sale",
            mx_provider=dns.mx_provider,
            pages_fetched=[home.final_url],
            jina_tokens=home.jina_tokens or 0,
        )

    # --- stage 3: discovery. Wave-1 robots/sitemap belong to the pre-redirect host,
    # so drop them when the homepage landed on a different site.
    same_site = final_domain == registrable(host)
    selected, diag = await discover_pages(
        fetcher,
        domain=domain,
        final_url=home.final_url,
        homepage_html=home.html,
        robots_text=robots_text if same_site else "",
        root_sitemap_text=sitemap_text if same_site else "",
        limit=max(0, settings.max_pages_per_domain - 1),
    )
    log.info(
        "discovered",
        extra={"domain": domain, "selected": [c.url for c in selected], **diag},
    )

    # --- stages 4-6: fetch the selected pages in the spec 16 wave order, extracting
    # as we go so the early stop can fire before wave 3 is spent.
    scratch.pages.append((home.final_url, 0, home.html))
    scratch.tokens += home.jina_tokens or 0
    pages = scratch.pages

    async def fetch_page(c) -> tuple[str, int, str, int]:
        try:
            r = await fetcher.get_html(c.url, domain=domain)
            return r.final_url, c.score, r.html, (r.jina_tokens or 0)
        except FetchError:
            return c.url, c.score, "", 0

    contact = next((c for c in selected if c.tier == "contact"), None)
    rest = [c for c in selected if c is not contact]

    if contact is not None:
        url, score, html, tok = await fetch_page(contact)
        scratch.tokens += tok
        if html:
            pages.append((url, score, html))
        # Spec 16.3: stop once a rank-1 mailto/JSON-LD address is in hand. Only correct
        # when the deliverable is one best_email; with COLLECT_ALL_EMAILS it would
        # discard every address living on the pages we skip.
        if settings.early_stop_on_first_good:
            early = rank_candidates(
                merge_pages([extract_page(h, u)[0] for u, _, h in pages]), final_domain
            )
            if early and early[0].rank == 1 and early[0].candidate.method in ("mailto", "jsonld"):
                log.info("early_stop", extra={"domain": domain, "saved_pages": len(rest)})
                rest = []

    if rest:
        for url, score, html, tok in await asyncio.gather(*(fetch_page(c) for c in rest)):
            scratch.tokens += tok
            if html:
                pages.append((url, score, html))

    judgment = await _judge_pages(domain, scratch)
    return _harvest(domain, scratch, judgment=judgment, started=started)


def _extract_all(scratch: Scratch):
    """Run stages 5-6 over whatever pages the scratchpad holds."""
    per_page: list[list[EmailCandidate]] = []
    phones: list[str] = []
    socials: dict[str, str] = {}
    for url, _score, html in scratch.pages:
        candidates, text, _title = extract_page(html, url)
        per_page.append(candidates)
        for phone in extract_phones(html, text):
            if phone not in phones:
                phones.append(phone)
        for network, link in extract_socials(html, url).items():
            socials.setdefault(network, link)

    page_scores = {url: score for url, score, _ in scratch.pages}
    site = scratch.final_domain or ""
    ranked = rank_candidates(merge_pages(per_page), site, page_scores)

    # Spec 12 provenance: every emitted address must carry a source URL we actually
    # fetched. A substring check against page text cannot be used -- it passes for a
    # truncated match like "private@" cut out of "_private@".
    fetched = {u for u, _, _ in scratch.pages}
    kept = []
    for r in ranked:
        if set(r.candidate.source_urls) & fetched:
            kept.append(r)
        else:
            log.error(
                "email_provenance_violation",
                extra={"email": r.email, "sources": r.candidate.source_urls},
            )
    return kept, phones, socials, pick_contact_form_url(scratch.pages)


async def _judge_pages(domain: str, scratch: Scratch):
    ranked, _p, _s, _f = _extract_all(scratch)
    from app.pipeline.extract.context import page_title

    title = page_title(scratch.home_html)
    return await _judge.judge(ranked, scratch.final_domain or domain, title)


def _harvest(
    domain: str,
    scratch: Scratch,
    *,
    judgment,
    started: float,
    error_reason: str | None = None,
) -> DomainResult:
    """Build the result from the scratchpad. `judgment=None` means rules only."""
    ranked, phones, socials, contact_form_url = _extract_all(scratch)
    if judgment is None:
        judgment = rules_only(ranked)

    best = judgment.best_email
    business = judgment.business_emails

    known = {r.email for r in ranked}
    if best and best not in known:
        log.error("typesafe_returned_unknown_email", extra={"domain": domain, "email": best})
        best = rules_best(ranked)
        judgment.best_email_source = "rules"
        judgment.confidence = None

    log.info(
        "domain_done",
        extra={
            "domain": domain,
            "final_domain": scratch.final_domain,
            "pages": len(scratch.pages),
            "candidates": len(ranked),
            "typesafe_called": judgment.typesafe_called,
            "jina_tokens": scratch.tokens,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "homepage_text_len": len(visible_text(scratch.home_html)),
            "partial": bool(error_reason),
        },
    )
    return _result(
        domain,
        final_domain=scratch.final_domain,
        status=assign_status(
            StatusInputs(
                parked=judgment.site_status == "parked_or_for_sale",
                typesafe_said_none=judgment.said_none,
                best_email=best,
                business_emails=business,
                contact_form_url=contact_form_url,
            )
        ),
        best_email=best,
        best_email_source=judgment.best_email_source if best else None,
        confidence=judgment.confidence,
        needs_review=judgment.needs_review or bool(error_reason),
        business_emails=business,
        all_emails=[
            EmailCandidateOut(
                email=r.email,
                source_urls=r.candidate.source_urls,
                method=r.candidate.method,
                rank=r.rank,
                belongs=judgment.belongs.get(r.email),
            )
            for r in ranked
        ],
        phones=phones[:5],
        socials=socials,
        contact_form_url=contact_form_url,
        mx_provider=scratch.dns.mx_provider if scratch.dns else None,
        pages_fetched=[u for u, _, _ in scratch.pages],
        jina_tokens=scratch.tokens,
        typesafe_called=judgment.typesafe_called,
        typesafe_error=judgment.typesafe_error,
        site_status=judgment.site_status,
        error_reason=error_reason,
    )


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()


async def process_input(raw: str, fetcher: Fetcher | None = None) -> DomainResult:
    """Normalise a raw input cell, then run the pipeline. Used by POST /extract."""
    norm = normalize_input(raw)
    if not norm.ok:
        return DomainResult(
            domain=norm.domain or raw.strip()[:255],
            status="invalid_input",
            error_reason=norm.reason,
        )
    result = await process_domain(norm.domain, fetcher)
    if norm.notes:
        result.error_reason = result.error_reason or norm.notes[0]
    return result
