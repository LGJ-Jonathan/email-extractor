"""Stage 3a robots.txt: pull Sitemap: lines, same registrable domain only, max 5.

The regex is line anchored, which is why `get_text` must not go through Jina -- its text
mode collapses newlines and this would match nothing on every domain (see fetch.py).
"""

import re

from app.pipeline.normalize import registrable

SITEMAP_LINE = re.compile(r"(?im)^\s*sitemap:\s*(\S+)")
MAX_SITEMAPS = 5


def parse_sitemaps(robots_text: str, site_host: str, *, limit: int = MAX_SITEMAPS) -> list[str]:
    """Sitemap URLs declared in robots.txt, restricted to the site's own domain."""
    if not robots_text:
        return []
    # A robots.txt that is actually an HTML error page has no Sitemap lines anyway,
    # but bail early so a page full of markup cannot produce junk.
    if robots_text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        return []

    own = registrable(site_host)
    out: list[str] = []
    seen: set[str] = set()
    for raw in SITEMAP_LINE.findall(robots_text):
        url = raw.strip().strip("<>\"'")
        if not url.lower().startswith(("http://", "https://")):
            continue
        from urllib.parse import urlsplit

        host = (urlsplit(url).hostname or "").lower()
        if not host or registrable(host) != own:
            continue
        if url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= limit:
            break
    return out
