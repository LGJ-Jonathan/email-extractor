"""Stage 6 filter and rank.

Corrections:

* **The deny list was an exact-match set, so rank 1 was defined by exclusion.** Any
  own-domain local part not in the 26-item list outranked `info@`: `no_reply@` (the list
  has `no-reply` and `noreply`, not the underscore), `donotreply2@`, `wordpress@`,
  `newsletter@`, `bounce@` and `root@` all landed at rank 1. The rule is now a pattern.
* **The `own` test's reverse clause was wrong-direction.** "one ends with `.` + the
  other" makes `bob@co.uk` own-domain for `acme.co.uk`. Only the forward direction is
  kept, plus the explicit section 15 sibling-brand rule.
* **`free` matched the first host label**, so `info@mail.bluepixel.com` was classified as
  a free-provider address (rank 3) instead of third party (rank 4). It now uses the
  registrable domain.
"""

import re
from dataclasses import dataclass

import tldextract

from app.pipeline.extract.emails import EmailCandidate

_extract = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)

MAX_CANDIDATES = 50

ASSET_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".css", ".js",
    ".json", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".webm", ".avif", ".tif", ".tiff",
)

BAD_DOMAINS = frozenset({
    "example.com", "example.org", "example.net", "domain.com", "yourdomain.com",
    "yoursite.com", "website.com", "email.com", "mysite.com", "test.com", "sentry.io",
    "wixpress.com", "sentry-next.wixpress.com", "wix.com", "squarespace.com",
    "godaddysites.com", "weebly.com", "wordpress.com", "shopify.com", "myshopify.com",
    "sentry.wixpress.com",
})

# A pattern, not a set: the set let every near-miss through at rank 1.
BAD_LOCAL = re.compile(
    r"(?i)^(?:"
    r"no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|mailer[-_.]?daemon|postmaster|"
    r"hostmaster|abuse|bounces?|mailer|daemon|cron|root|wordpress|wp|newsletter|"
    r"unsubscribe|notifications?|automated|noreply\d*|"
    r"name|your[-_.]?name|you|user|username|email|your[-_.]?email|youremail|"
    r"first[-_.]?name|last[-_.]?name|firstname[-_.]?lastname|first[-_.]?last|"
    r"john[-_.]?doe|jane[-_.]?doe|john[-_.]?smith|johndoe|janedoe|johnsmith|"
    r"test|example|sample|demo|placeholder"
    r")(?:[-_.+]?\d*)$"
)

ROLE_LOCALS = frozenset({
    "info", "hello", "contact", "sales", "support", "office", "admin", "team", "service",
    "help", "inquiries", "enquiries", "booking", "bookings", "appointments", "estimates",
    "quotes", "marketing", "hr", "careers", "jobs", "billing", "accounts", "privacy",
    "legal", "media", "press", "webmaster",
})

FREE_LABELS = frozenset({
    "gmail", "yahoo", "hotmail", "outlook", "aol", "icloud", "msn", "live", "comcast",
    "att", "sbcglobal", "verizon", "bellsouth", "me", "ymail", "protonmail", "proton",
    "mail", "gmx", "zoho", "yandex",
})

_HEXISH = re.compile(r"^[a-f0-9]{12,}$", re.I)
_DIGITS = re.compile(r"^\d+$")
SIBLING_MIN_LABEL = 5


@dataclass
class RankedEmail:
    candidate: EmailCandidate
    rank: int
    own: bool
    role: bool
    free: bool

    @property
    def email(self) -> str:
        return self.candidate.email


def local_and_domain(email: str) -> tuple[str, str]:
    local, _, domain = email.rpartition("@")
    return local.lower(), domain.lower()


def should_drop(email: str) -> str | None:
    """Reason to drop, or None to keep."""
    if len(email) > 254:
        return "too_long"
    if email.endswith(ASSET_SUFFIXES):
        return "asset"
    local, domain = local_and_domain(email)
    if not local or not domain:
        return "malformed"
    if len(local) > 64:
        return "local_too_long"
    if domain in BAD_DOMAINS:
        return "bad_domain"
    root = _extract(domain).registered_domain
    if root and root in BAD_DOMAINS:
        return "bad_domain"
    if BAD_LOCAL.match(local):
        return "bad_local"
    if _HEXISH.match(local) or _DIGITS.match(local):
        return "opaque_local"
    return None


def _second_level(domain: str) -> str:
    return _extract(domain).domain.lower()


def is_own(email_domain: str, site_domain: str) -> bool:
    """Same site, a subdomain of it, or a section 15 sibling brand."""
    if not site_domain:
        return False
    site = site_domain.lower()
    host = email_domain.lower()
    if host == site or host.endswith("." + site):
        return True
    a, b = _second_level(host), _second_level(site)
    if len(a) >= SIBLING_MIN_LABEL and len(b) >= SIBLING_MIN_LABEL and (a in b or b in a):
        return True
    return False


def is_free(email_domain: str) -> bool:
    return _second_level(email_domain) in FREE_LABELS


def classify(email: str, site_domain: str) -> tuple[bool, bool, bool, int]:
    local, domain = local_and_domain(email)
    own = is_own(domain, site_domain)
    role = local in ROLE_LOCALS
    free = is_free(domain)
    if own and not role:
        rank = 1
    elif own and role:
        rank = 2
    elif free:
        rank = 3
    else:
        rank = 4
    return own, role, free, rank


def rank_candidates(
    candidates: list[EmailCandidate],
    site_domain: str,
    page_scores: dict[str, int] | None = None,
) -> list[RankedEmail]:
    """Drop, classify, then order. Ties: mailto/jsonld first, higher-scored page, order."""
    page_scores = page_scores or {}
    ranked: list[RankedEmail] = []
    for c in candidates:
        if should_drop(c.email):
            continue
        own, role, free, rank = classify(c.email, site_domain)
        ranked.append(RankedEmail(candidate=c, rank=rank, own=own, role=role, free=free))

    def best_page_score(r: RankedEmail) -> int:
        return max((page_scores.get(u, 0) for u in r.candidate.source_urls), default=0)

    ranked.sort(
        key=lambda r: (
            r.rank,
            0 if r.candidate.method in ("mailto", "jsonld") else 1,
            -best_page_score(r),
            r.candidate.order,
            r.email,
        )
    )
    return ranked[:MAX_CANDIDATES]


def rules_best(ranked: list[RankedEmail]) -> str | None:
    return ranked[0].email if ranked else None
