"""Parked / for-sale detection (spec section 6, deterministic, after the homepage fetch).

Resolved ambiguity: the spec's third rule reads "text contains any of [seven phrases] AND
visible text is under 300 characters", and the AND's scope is undefined. Binding it to the
whole list means an explicit "this domain is for sale" page with 400 characters of parking
boilerplate is missed and gets fully crawled -- thousands of wasted fetches in a 50k list.
Binding it to nothing means a one-page business site saying "future home of our new
location" is wrongly killed. Split accordingly:

  * an explicit for-sale phrase is conclusive at any length
  * a weak phrase ("coming soon") needs the under-300-character guard

Flagged in the step 3 report; revert to a single rule if you prefer the literal reading.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser

PARKING_NS_MARKERS = (
    "sedoparking", "bodis", "parkingcrew", "above.com", "dan.com", "afternic",
    "hugedomains", "namebrightdns", "parklogic",
)

PARKING_HOSTS = (
    "dan.com", "afternic.com", "hugedomains.com", "sedo.com", "undeveloped.com",
)
PARKING_HOST_PATHS = {"godaddy.com": ("/forsale",)}

FOR_SALE_PHRASES = (
    "this domain is for sale",
    "buy this domain",
    "domain may be for sale",
    "this domain has been registered",
)
WEAK_PHRASES = ("parked free", "future home of", "coming soon")
WEAK_MAX_CHARS = 300

_WS = re.compile(r"\s+")


def visible_text(html: str) -> str:
    """Rough visible text. Stage 5d builds the richer version; this only feeds parking rules."""
    if not html:
        return ""
    try:
        tree = HTMLParser(html)
    except Exception:  # noqa: BLE001
        return _WS.sub(" ", re.sub(r"<[^>]+>", " ", html)).strip()
    for tag in ("script", "style", "svg", "noscript", "template"):
        for node in tree.css(tag):
            node.decompose()
    body = tree.body or tree.root
    return _WS.sub(" ", body.text(separator=" ") if body else "").strip()


@dataclass
class ParkedVerdict:
    parked: bool = False
    signal: str | None = None       # ns | host | phrase

    def __bool__(self) -> bool:
        return self.parked


def check_ns(ns_hosts: list[str]) -> ParkedVerdict:
    for ns in ns_hosts:
        low = ns.lower()
        if any(m in low for m in PARKING_NS_MARKERS):
            return ParkedVerdict(True, "ns")
    return ParkedVerdict()


def check_final_url(final_url: str) -> ParkedVerdict:
    host = (urlsplit(final_url).hostname or "").lower()
    path = urlsplit(final_url).path or ""
    for h in PARKING_HOSTS:
        if host == h or host.endswith("." + h):
            return ParkedVerdict(True, "host")
    for h, prefixes in PARKING_HOST_PATHS.items():
        if (host == h or host.endswith("." + h)) and any(path.startswith(p) for p in prefixes):
            return ParkedVerdict(True, "host")
    return ParkedVerdict()


def check_text(html_or_text: str, *, already_text: bool = False) -> ParkedVerdict:
    text = html_or_text if already_text else visible_text(html_or_text)
    low = text.lower()
    if any(p in low for p in FOR_SALE_PHRASES):
        return ParkedVerdict(True, "phrase")
    if len(text) < WEAK_MAX_CHARS and any(p in low for p in WEAK_PHRASES):
        return ParkedVerdict(True, "phrase")
    return ParkedVerdict()


def is_parked(
    *, ns_hosts: list[str] | None = None, final_url: str = "", html: str = ""
) -> ParkedVerdict:
    """Any one signal is enough. Parked domains skip stages 3 to 7."""
    for verdict in (
        check_ns(ns_hosts or []),
        check_final_url(final_url) if final_url else ParkedVerdict(),
        check_text(html) if html else ParkedVerdict(),
    ):
        if verdict.parked:
            return verdict
    return ParkedVerdict()
