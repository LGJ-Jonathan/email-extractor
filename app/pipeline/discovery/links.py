"""Stage 3c homepage links: every same-domain <a href> with its anchor text."""

import re
from urllib.parse import urldefrag, urljoin, urlsplit

from selectolax.parser import HTMLParser

from app.pipeline.normalize import registrable

_WS = re.compile(r"\s+")
SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "sms:", "fax:", "data:", "file:", "callto:")


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """(absolute_url, anchor_text) for same-registrable-domain links, in document order."""
    if not html:
        return []
    try:
        tree = HTMLParser(html)
    except Exception:  # noqa: BLE001
        return []

    own = registrable((urlsplit(base_url).hostname or "").lower())
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        if href.lower().startswith(SKIP_SCHEMES):
            continue
        try:
            absolute = urldefrag(urljoin(base_url, href))[0]
        except ValueError:
            continue
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        host = (parts.hostname or "").lower()
        if not host or registrable(host) != own:
            continue
        anchor = _WS.sub(" ", node.text(separator=" ") or "").strip().lower()[:120]
        key = absolute
        if key in seen:
            continue
        seen.add(key)
        out.append((absolute, anchor))
    return out
