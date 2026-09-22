"""Stage 5c: every email source, merged by method precedence."""

import re
from dataclasses import dataclass, field
from urllib.parse import unquote

import tldextract
from selectolax.parser import HTMLParser

from app.pipeline.extract import jsonld as jsonld_mod
from app.pipeline.extract.cloudflare import decode_html
from app.pipeline.extract.context import context_for, page_title, visible_text
from app.pipeline.extract.normalize_html import normalize_html
from app.pipeline.extract.patterns import EMAIL, EMAIL_FULL, clean
from app.pipeline.htmlscan import bodies

_extract = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)

# Highest precision first (spec 5c).
METHOD_PRECEDENCE = ("mailto", "jsonld", "cloudflare", "text", "attribute", "script")
_RANK = {m: i for i, m in enumerate(METHOD_PRECEDENCE)}


@dataclass
class EmailCandidate:
    email: str
    method: str
    source_urls: list[str] = field(default_factory=list)
    context: str = ""
    order: int = 0        # first-appearance tiebreak within a page


# Only these are trimmed back to. Trimming to *any* valid suffix fabricates addresses:
# "sha256@abc.def" became "sha256@abc.de" (Germany), "logo@2x.png" became "logo@2x.pn"
# (Pitcairn) and "hero@2x.jpg" became "hero@2x.jp" (Japan) -- none of which appeared on
# the page. Section 15's rule exists for text glued onto a real address, so the real
# address must still be a prefix ending in a common TLD.
TRIM_TARGETS = ("com", "net", "org", "edu", "gov", "mil", "int", "io", "co", "us", "biz", "info")
_ASSET_TAIL = re.compile(
    r"(?i)\.(?:png|jpe?g|gif|webp|svg|bmp|ico|css|js|json|woff2?|ttf|eot|mp4|webm|avif|tiff?)$"
)


# `info@acme.com.Call us` -- the sentence runs straight on after the address and
# `.call` is a real TLD, so the suffix check passed and `info@acme.com.call` came out.
# Only a capitalised word glued after a common TLD is trimmed; `bob@acme.co.uk` and
# `x@acme.com.au` have lowercase labels and are left alone.
_GLUED_SENTENCE = re.compile(
    r"^(.+\.(?:" + "|".join(TRIM_TARGETS) + r"))\.[A-Z][a-z]+$"
)


def unglue_sentence(raw: str) -> str:
    m = _GLUED_SENTENCE.match(raw.strip().strip(".,;:!?)]}>\"'"))
    return m.group(1) if m else raw


def _version_like(domain: str) -> bool:
    """`18.2.0-canary.abc` is a package spec, not a host: several labels, some numeric."""
    labels = domain.split(".")
    body = labels[:-1]
    return len(body) >= 2 and any(label.isdigit() for label in body)


def fix_tld(email: str) -> str | None:
    """Spec 15: `info@acme.comCall us` -> trim the glued text back to the real suffix."""
    local, _, domain = email.rpartition("@")
    if not local or not domain:
        return None
    if _ASSET_TAIL.search(domain) or _version_like(domain):
        return None
    if _extract(domain).suffix:
        return email
    labels = domain.split(".")
    last = labels[-1].lower()
    for target in TRIM_TARGETS:
        tail = last[len(target):]
        # A one-letter tail is more often a TLD tldextract does not know (`.usa`)
        # than glued text; trimming it rewrote `info@acme.usa` to `info@acme.us`.
        if last.startswith(target) and len(tail) >= 2 and tail.isalpha():
            candidate = ".".join(labels[:-1] + [target])
            if _extract(candidate).suffix:
                return f"{local}@{candidate}"
    return None


def _accept(raw: str) -> str | None:
    email = clean(unglue_sentence(raw))
    if not email or not EMAIL_FULL.fullmatch(email):
        return None
    return fix_tld(email)


def _mailto_addresses(href: str) -> list[str]:
    """Spec 15: percent-decoding, several recipients, cc/bcc params."""
    body = href[7:] if href[:7].lower() == "mailto:" else href
    body = unquote(body).strip()
    head, _, query = body.partition("?")
    parts = [p for p in re.split(r"[,;]", head) if p.strip()]
    for key in ("cc=", "bcc=", "to="):
        for m in re.finditer(rf"(?i)\b{key}([^&]+)", query):
            parts.extend(re.split(r"[,;]", unquote(m.group(1))))
    return [p.strip() for p in parts if p.strip()]


def extract_page(html: str, url: str) -> tuple[list[EmailCandidate], str, str]:
    """Return (candidates, visible_text, title) for one fetched page."""
    if not html:
        return [], "", ""

    decoded, cf_emails = decode_html(html)
    normalised = normalize_html(decoded)
    text = visible_text(normalised)
    title = page_title(normalised)

    found: dict[str, EmailCandidate] = {}
    counter = 0

    def add(raw: str, method: str) -> None:
        nonlocal counter
        email = _accept(raw)
        if not email:
            return
        if email in cf_emails and method in ("text", "mailto", "attribute", "script"):
            method = "cloudflare"
        existing = found.get(email)
        if existing is None:
            counter += 1
            found[email] = EmailCandidate(
                email=email, method=method, source_urls=[url], order=counter
            )
        elif _RANK[method] < _RANK[existing.method]:
            existing.method = method

    try:
        tree = HTMLParser(normalised)
    except Exception:  # noqa: BLE001
        tree = None

    # mailto
    if tree is not None:
        for node in tree.css("a[href]"):
            href = (node.attributes.get("href") or "").strip()
            if href[:7].lower() == "mailto:":
                for address in _mailto_addresses(href):
                    add(address, "mailto")

    # jsonld (parsed from the untouched blocks), then its unparseable bodies by regex
    good, failed = jsonld_mod.extract(normalised)
    for address in good:
        add(address, "jsonld")
    for body in failed:
        for address in jsonld_mod.regex_emails(body):
            add(address, "script")

    # text
    for address in EMAIL.findall(text):
        add(address, "text")

    # attributes
    if tree is not None:
        for node in tree.css("*"):
            for value in node.attributes.values():
                if value and "@" in value:
                    for address in EMAIL.findall(value):
                        add(address, "attribute")

    # remaining inline script bodies
    for body in bodies(normalised, "script"):
        for address in EMAIL.findall(body):
            add(address, "script")

    candidates = sorted(found.values(), key=lambda c: c.order)
    for candidate in candidates:
        candidate.context = context_for(text, candidate.email)
    return candidates, text, title


def merge_pages(per_page: list[list[EmailCandidate]]) -> list[EmailCandidate]:
    """One candidate per address across pages, best method kept, all source URLs kept."""
    merged: dict[str, EmailCandidate] = {}
    for page_index, candidates in enumerate(per_page):
        for c in candidates:
            existing = merged.get(c.email)
            if existing is None:
                merged[c.email] = EmailCandidate(
                    email=c.email,
                    method=c.method,
                    source_urls=list(c.source_urls),
                    context=c.context,
                    order=page_index * 10_000 + c.order,
                )
                continue
            for u in c.source_urls:
                if u not in existing.source_urls:
                    existing.source_urls.append(u)
            if _RANK[c.method] < _RANK[existing.method]:
                existing.method = c.method
            if not existing.context and c.context:
                existing.context = c.context
    return sorted(merged.values(), key=lambda c: c.order)
