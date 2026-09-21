"""Stage 3b sitemap parsing.

Two corrections to the literal spec, each tested:

1. **Child sitemap filtering matches whole tokens of the last path segment, not
   substrings of the whole URL.** The URL contains the host, so `tag` excluded
   heritageroofing.com, cottagekitchens.com, advantagehvac.com and stagecoachmoving.com;
   `author` excluded authorityplumbing.com; `news` excluded newsomeelectric.com. One
   unlucky brand name killed all sitemap discovery for the domain.
2. **The `<loc>` regex handles CDATA, namespace prefixes and attributes.** The spec's
   justification for a regex is that sitemaps are malformed, but its pattern failed on
   exactly the malformed shapes it exists for.

Also: robots-listed sitemaps are all fetched (up to the cap) and unioned rather than
stopping at the first valid one -- a product sitemap listed before a page sitemap would
otherwise hide the contact page while still suppressing the fixed-path fallback.
"""

import gzip
import html
import logging
import re

from app.pipeline.discovery.score import segments, tokens

log = logging.getLogger("email_extractor.sitemap")

MAX_URLS = 5_000
MAX_CHILDREN = 3
MAX_DEPTH = 2

CANDIDATE_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/page-sitemap.xml")

EXCLUDE_CHILD_TOKENS = frozenset({
    "product", "products", "post", "posts", "blog", "news", "image", "images",
    "video", "videos", "category", "categories", "tag", "tags", "author", "authors",
})
PREFER_CHILD_TOKEN = "page"

_LOC = re.compile(
    r"(?is)<(?:\w+:)?loc[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</(?:\w+:)?loc\s*>"
)
_IS_INDEX = re.compile(r"(?i)<(?:\w+:)?sitemapindex[\s>]")


def maybe_gunzip(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw).decode("utf-8", errors="replace")
        except OSError:
            return ""
    return raw.decode("utf-8", errors="replace")


def is_sitemap_index(text: str) -> bool:
    """Look only at the head, so a stylesheet reference or comment cannot fool it."""
    return bool(_IS_INDEX.search(text[:2048]))


def parse_locs(text: str) -> list[str]:
    """Every <loc> value, entity-decoded. Captures containing markup are dropped."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _LOC.findall(text):
        value = html.unescape(raw.strip())
        if "<" in value:
            # "<loc>a<loc>b</loc>" swallowed the next opening tag; recover the innermost
            # value rather than losing both entries.
            value = html.unescape(re.split(r"(?i)<(?:\w+:)?loc[^>]*>", value)[-1].strip())
        if not value or "<" in value or ">" in value:
            continue
        if not value.lower().startswith(("http://", "https://")):
            continue
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def parse_plain_text(text: str) -> list[str]:
    """A plain-text sitemap is one URL per line."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith(("http://", "https://")) and " " not in line:
            out.append(line)
    return out


def _last_segment_tokens(url: str) -> set[str]:
    from urllib.parse import urlsplit

    segs = segments(urlsplit(url).path or "")
    if not segs:
        return set()
    last = segs[-1].lower()
    # Split on dots too so "product-sitemap.xml" yields {product, sitemap, xml}.
    parts: set[str] = set()
    for chunk in last.split("."):
        parts.update(tokens(chunk))
    return parts


def child_is_excluded(url: str) -> bool:
    return bool(_last_segment_tokens(url) & EXCLUDE_CHILD_TOKENS)


def order_children(urls: list[str]) -> list[str]:
    """Page-ish children first; everything excluded is already gone."""
    keep = [u for u in urls if not child_is_excluded(u)]
    preferred = [u for u in keep if PREFER_CHILD_TOKEN in _last_segment_tokens(u)]
    rest = [u for u in keep if u not in set(preferred)]
    return preferred + rest


def extract_urls(text: str) -> tuple[list[str], bool]:
    """Return (urls, is_index). Falls back to plain-text parsing when there is no <loc>."""
    locs = parse_locs(text)
    if locs:
        return locs, is_sitemap_index(text)
    return parse_plain_text(text), False
