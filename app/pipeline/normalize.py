"""Stage 1 normalisation and every section 15 input case.

The important departure from a naive reading of Stage 1: a registrable domain is the
WRONG crawl key for site-builder hosts. `tldextract` maps `bobshvac.squarespace.com` to
`squarespace.com`, so with `domains.domain` as the primary key every Squarespace-hosted
business in a 50k list would collapse into one row, be crawled once, and all receive
Squarespace's own result -- then cached for CACHE_DAYS. Section 15 forbids exactly this.

`include_psl_private_domains=True` (spec section 1) fixes some of these via the PSL's
private section but NOT squarespace.com, godaddysites.com or weebly.com, so an explicit
list is required on top of it. Verified per host in tests.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import tldextract

# Bundled suffix list only, private section on (spec section 1).
_extract = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)

NULL_TOKENS = {
    "", "-", "--", "n/a", "na", "none", "null", "nil", "#n/a", "#na",
    "not available", "unknown", "tbd", ".",
}

# Section 15: the crawl key is the FULL host, never the platform root.
SITE_BUILDER_SUFFIXES = (
    "wixsite.com", "squarespace.com", "godaddysites.com", "weebly.com", "square.site",
    "webflow.io", "wordpress.com", "blogspot.com", "myshopify.com", "carrd.co",
    "editorx.io",
)
# Only wixsite carries the site name in the first path segment.
PATH_SCOPED_SUFFIXES = ("wixsite.com",)

# Section 15: these are not the business's own website.
SOCIAL_HOSTS = (
    "facebook.com", "fb.com", "instagram.com", "linkedin.com", "yelp.com", "g.page",
    "business.site", "linktr.ee", "x.com", "twitter.com", "youtube.com", "angi.com",
    "homeadvisor.com", "thumbtack.com", "bbb.org", "nextdoor.com", "tiktok.com",
)
SOCIAL_PATH_HOSTS = {"google.com": ("/maps",), "goo.gl": ("/maps",)}

# Stage 6 free-provider list, reused by the section 14 email fallback.
FREE_EMAIL_LABELS = {
    "gmail", "yahoo", "hotmail", "outlook", "aol", "icloud", "msn", "live", "comcast",
    "att", "sbcglobal", "verizon", "bellsouth", "me", "ymail", "protonmail", "mail",
}

_SEPARATORS = re.compile(r"[,;|\n\r\t]+|\s+/\s+")
_HOSTISH = re.compile(
    r"[A-Za-z0-9¡-￿](?:[A-Za-z0-9¡-￿._-]*[A-Za-z0-9¡-￿])?"
    r"\.[A-Za-z¡-￿]{2,24}"
)
_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
# urlsplit will happily return "acme (acmeroofing.com" as a hostname, so validate.
_VALID_HOST = re.compile(
    r"^[a-z0-9\u00a1-\uffff](?:[a-z0-9\u00a1-\uffff.-]*[a-z0-9\u00a1-\uffff])?$"
)
_WWW = re.compile(r"^www\d*\.")


@dataclass
class NormalizedInput:
    """The outcome of normalising one input cell."""

    domain: str | None = None           # the crawl key and domains.domain value
    display: str = ""                   # unicode form for display (IDN kept readable)
    status: str | None = None           # "invalid_input" or None
    reason: str | None = None           # section 17 error_reason
    domain_source: str = "website"      # website | email
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.domain is not None and self.status is None


def _fragments(value: str) -> list[str]:
    parts = [p.strip() for p in _SEPARATORS.split(value) if p.strip()]
    return parts or ([value.strip()] if value.strip() else [])


def _tokens(fragment: str) -> list[str]:
    """Candidate host-bearing strings, most specific first."""
    cleaned = fragment.strip(" \t\"'`()[]{}<>")
    out = [cleaned] if cleaned else []
    out.extend(m.group(0) for m in _HOSTISH.finditer(fragment))
    return out


def _host_and_path(token: str) -> tuple[str, str] | None:
    candidate = token if "//" in token else "http://" + token.lstrip("/")
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    if not _VALID_HOST.match(host) or ".." in host:
        return None
    if any(not label for label in host.split(".")):
        return None
    return host, parts.path or ""


def _to_punycode(host: str) -> str | None:
    try:
        return host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        try:
            return host.encode("utf-8").decode("ascii")
        except UnicodeDecodeError:
            return None


def _site_builder_suffix(host: str) -> str | None:
    for suffix in SITE_BUILDER_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return suffix
    return None


def _is_social(host: str, path: str) -> bool:
    for h in SOCIAL_HOSTS:
        if host == h or host.endswith("." + h):
            return True
    for h, prefixes in SOCIAL_PATH_HOSTS.items():
        if (host == h or host.endswith("." + h)) and any(path.startswith(p) for p in prefixes):
            return True
    return False


def registrable(host: str) -> str:
    """Crawl key for a host: the full host for site builders, else the registrable domain."""
    if _site_builder_suffix(host):
        return host
    return _extract(host).registered_domain or ""


def normalize_input(raw: str) -> NormalizedInput:
    """Turn one input cell into a crawl key, or explain why it is not one."""
    value = (raw or "").strip()
    display = value
    if value.lower() in NULL_TOKENS:
        return NormalizedInput(display=display, status="invalid_input", reason="empty")

    frags = _fragments(value)
    if not frags:
        return NormalizedInput(display=display, status="invalid_input", reason="empty")

    notes: list[str] = []
    if len(frags) > 1:
        notes.append("multiple_values")

    for fragment in frags:
        result = _normalize_fragment(fragment, display, notes)
        if result is not None:
            return result

    # Nothing domain-shaped anywhere in the cell.
    reason = "not_a_url" if (" " in value or "." not in value) else "not_a_url"
    return NormalizedInput(display=display, status="invalid_input", reason=reason, notes=notes)


def _normalize_fragment(fragment: str, display: str, notes: list[str]) -> NormalizedInput | None:
    source = "website"
    work = fragment

    # Section 15: an email in the website column contributes its domain.
    if "@" in work and not work.lower().startswith(("http://", "https://")):
        local, _, rest = work.rpartition("@")
        if local and "." in rest:
            label = _extract(rest.strip().lower().rstrip(".")).domain
            if label in FREE_EMAIL_LABELS:
                return NormalizedInput(
                    display=display, status="invalid_input",
                    reason="free_email_only", domain_source="email", notes=list(notes),
                )
            work = rest
            source = "email"

    for token in _tokens(work):
        parsed = _host_and_path(token)
        if parsed is None:
            continue
        host, path = parsed
        if _IPV4.match(host):
            return NormalizedInput(
                display=display, status="invalid_input", reason="not_a_url", notes=list(notes)
            )

        host = _WWW.sub("", host)
        ascii_host = _to_punycode(host)
        if not ascii_host or "." not in ascii_host:
            continue

        if _is_social(ascii_host, path):
            return NormalizedInput(
                display=display, status="invalid_input",
                reason="social_or_directory", notes=list(notes),
            )

        suffix = _site_builder_suffix(ascii_host)
        if suffix:
            if ascii_host == suffix:
                # Bare platform root is not a business site.
                return NormalizedInput(
                    display=display, status="invalid_input",
                    reason="social_or_directory", notes=list(notes),
                )
            key = ascii_host
            if suffix in PATH_SCOPED_SUFFIXES:
                first = next((s for s in path.split("/") if s), "")
                if first:
                    key = f"{ascii_host}/{first}"
            return NormalizedInput(
                domain=key, display=display, domain_source=source, notes=list(notes)
            )

        root = _extract(ascii_host).registered_domain
        if root:
            return NormalizedInput(
                domain=root, display=display, domain_source=source, notes=list(notes)
            )

    return None


def crawl_url(domain: str, *, scheme: str = "https", www: bool = False) -> str:
    """Build a fetchable URL from a crawl key (which may carry a wixsite path segment)."""
    host, _, path = domain.partition("/")
    prefix = "www." if www else ""
    return f"{scheme}://{prefix}{host}/" + (f"{path}/" if path else "")


def host_of_key(domain: str) -> str:
    return domain.partition("/")[0]
