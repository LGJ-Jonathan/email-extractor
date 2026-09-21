"""Stage 5d: visible text, per-email context windows, and the page title."""

import html as html_mod
import re

from selectolax.parser import HTMLParser

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_JSONLD = re.compile(r"""type\s*=\s*["']?application/ld\+json""", re.I)
CONTEXT_CHARS = 120
TITLE_CHARS = 150


def visible_text(html: str) -> str:
    """Tags to spaces, mailto anchors replaced by their address, entities unescaped."""
    if not html:
        return ""
    try:
        tree = HTMLParser(html)
    except Exception:  # noqa: BLE001
        return _WS.sub(" ", html_mod.unescape(_TAG.sub(" ", html))).strip()

    for tag in ("style", "svg", "noscript", "template", "head"):
        for node in tree.css(tag):
            node.decompose()
    for node in tree.css("script"):
        attrs = " ".join(f'{k}="{v}"' for k, v in node.attributes.items() if v)
        if not _JSONLD.search(attrs):
            node.decompose()

    # A mailto anchor whose text is "Email us" still needs the address in the stream,
    # otherwise its context window comes back empty.
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if href.lower().startswith("mailto:"):
            address = href[7:].split("?")[0].strip()
            if address:
                node.replace_with(f" {address} ")

    body = tree.body or tree.root
    text = body.text(separator=" ") if body else ""
    return _WS.sub(" ", html_mod.unescape(text)).strip()


def page_title(html: str) -> str:
    if not html:
        return ""
    try:
        tree = HTMLParser(html)
        node = tree.css_first("title")
        if node is None:
            return ""
        return _WS.sub(" ", html_mod.unescape(node.text() or "")).strip()[:TITLE_CHARS]
    except Exception:  # noqa: BLE001
        return ""


def context_for(text: str, email: str, *, window: int = CONTEXT_CHARS) -> str:
    """`window` characters either side of the first occurrence, trimmed."""
    if not text or not email:
        return ""
    idx = text.lower().find(email.lower())
    if idx < 0:
        return ""
    start = max(0, idx - window)
    end = min(len(text), idx + len(email) + window)
    return text[start:end].strip()
