"""Stage 3 page discovery: robots, sitemaps, homepage links, scoring and selection."""

import asyncio
import logging
from urllib.parse import urljoin, urlsplit

from app.pipeline.discovery.links import extract_links
from app.pipeline.discovery.robots import parse_sitemaps
from app.pipeline.discovery.sitemap import (
    MAX_CHILDREN,
    MAX_DEPTH,
    MAX_URLS,
    extract_urls,
    order_children,
)
from app.pipeline.discovery.score import ScoredUrl, has_tier, score_url, select
from app.pipeline.normalize import registrable

log = logging.getLogger("email_extractor.discovery")

FIXED_FALLBACK_PATHS = ("/contact", "/contact-us", "/about", "/about-us")
MAX_SITEMAP_DOCS = 4            # beyond the one fetched in wave 1
MIN_SCORED_BEFORE_FALLBACK = 2  # below this, try the fixed paths


async def fetch_text_or_blank(fetcher, url: str, domain: str) -> str:
    """Discovery fetches never fail a domain (spec 15: ignore and continue)."""
    try:
        return (await fetcher.get_text(url, domain=domain)).html
    except Exception as e:  # noqa: BLE001
        log.debug("discovery_fetch_failed", extra={"url": url, "err": type(e).__name__})
        return ""


def _same_site(url: str, own: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return bool(host) and registrable(host) == own


async def collect_sitemap_urls(
    fetcher, *, domain: str, own: str, seed_texts: list[str], seed_urls: list[str]
) -> list[str]:
    """Union of page URLs from every sitemap we are willing to read."""
    pages: list[str] = []
    seen_docs: set[str] = set()
    budget = MAX_SITEMAP_DOCS

    queue: list[tuple[str, int]] = [(u, 0) for u in seed_urls]
    texts: list[tuple[str, int]] = [(t, 0) for t in seed_texts if t]

    while texts or queue:
        if texts:
            text, depth = texts.pop(0)
        else:
            if budget <= 0:
                break
            url, depth = queue.pop(0)
            if url in seen_docs or not _same_site(url, own):
                continue
            seen_docs.add(url)
            budget -= 1
            text = await fetch_text_or_blank(fetcher, url, domain)
            if not text:
                continue

        urls, is_index = extract_urls(text)
        if is_index:
            if depth >= MAX_DEPTH:
                continue
            children = order_children([u for u in urls if _same_site(u, own)])[:MAX_CHILDREN]
            queue.extend((c, depth + 1) for c in children)
            continue

        for u in urls:
            if _same_site(u, own):
                pages.append(u)
            if len(pages) >= MAX_URLS:
                return pages
    return pages


async def discover_pages(
    fetcher,
    *,
    domain: str,
    final_url: str,
    homepage_html: str,
    robots_text: str = "",
    root_sitemap_text: str = "",
    limit: int,
) -> tuple[list[ScoredUrl], dict]:
    """Stage 3. Returns the pages to fetch and a small diagnostics dict."""
    own = registrable((urlsplit(final_url).hostname or "").lower())
    diag: dict = {"shortcut": False, "from_links": 0, "from_sitemap": 0, "fallback": False}

    # 3c: homepage links
    candidates: list[ScoredUrl] = []
    for url, anchor in extract_links(homepage_html, final_url):
        scored = score_url(url, anchor)
        if scored is not None:
            candidates.append(scored)
    diag["from_links"] = len(candidates)

    # Spec 16.2: a contact page plus an about/team page from the homepage alone is
    # enough; skip every further sitemap fetch.
    if has_tier(candidates, "contact") and has_tier(candidates, "about"):
        diag["shortcut"] = True
        return select(candidates, limit), diag

    # 3a + 3b: sitemaps
    seed_urls = parse_sitemaps(robots_text, own)
    sitemap_pages = await collect_sitemap_urls(
        fetcher,
        domain=domain,
        own=own,
        seed_texts=[root_sitemap_text],
        seed_urls=seed_urls,
    )
    before = len(candidates)
    for url in sitemap_pages:
        scored = score_url(url)
        if scored is not None:
            candidates.append(scored)
    diag["from_sitemap"] = len(candidates) - before

    chosen = select(candidates, limit)

    # Spec 3d fallback, widened twice over: the spec fires it only when there is no
    # sitemap at all, so a domain whose sitemap held nothing useful got no contact page
    # and no fallback. And only REAL tier matches count towards the threshold -- an
    # anchor-only match (score 10, no tier) is a weak signal and must not suppress the
    # known-good fixed paths, which score 100.
    if sum(1 for c in chosen if c.tier) < MIN_SCORED_BEFORE_FALLBACK:
        diag["fallback"] = True
        base = f"{urlsplit(final_url).scheme}://{urlsplit(final_url).netloc}"
        for path in FIXED_FALLBACK_PATHS:
            scored = score_url(urljoin(base + "/", path.lstrip("/")))
            if scored is not None:
                candidates.append(scored)
        chosen = select(candidates, limit)

    return chosen, diag
