"""Stage 3d URL scoring and selection.

Four corrections to the literal spec text, each with a test naming the case it fixes:

1. **Exclusions are anchored segment matches on the raw path, not substrings after
   normalisation.** The spec normalises trailing slashes away first and then excludes
   paths "containing `/blog/`", so every blog, news, category and product *index* page
   (`/blog`) survived and competed for the four page slots.
2. **The year rule is a whole-segment match.** `/20\\d\\d/` missed `/2024-annual-report`
   and wrongly matched `/store/2000/widget` (a product id).
3. **File extensions use an asset blocklist, not an allowlist.** "extensions other than
   none/.html/.htm/.php/.aspx" excludes any path with a dot in its last segment --
   `/locations/st.-louis`, `/about/dr.-smith` -- and every `.asp`, `.cfm`, `.jsp` and
   `.shtml` site, which is a large share of the older US small-business tail.
4. **Selection is deterministic and canonical.** URLs are canonicalised before dedupe
   (scheme, `www.`, case, `index.html`, query) so the same page cannot occupy several of
   the four slots, the sort has `url` as a final tiebreak, and the per-tier cap is 2 --
   "at most one per tier" made a second contact page unreachable while forcing a legal
   page into the crawl.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

TIERS: tuple[tuple[str, int, frozenset[str]], ...] = (
    ("contact", 100, frozenset({"contact", "contact-us", "contactus", "get-in-touch", "reach-us"})),
    ("quote", 80, frozenset({
        "quote", "get-a-quote", "request-a-quote", "free-estimate", "estimate",
        "schedule", "book", "booking", "appointment",
    })),
    ("about", 60, frozenset({
        "about", "about-us", "our-story", "team", "our-team", "leadership", "staff",
        "meet-the-team", "company", "who-we-are",
    })),
    ("legal", 40, frozenset({
        "privacy", "privacy-policy", "accessibility", "terms", "terms-of-service", "legal",
    })),
    ("locations", 20, frozenset({"locations", "location", "offices", "careers", "jobs"})),
)

ANCHOR_KEYWORDS: tuple[str, ...] = (
    "contact", "get in touch", "quote", "estimate", "schedule", "about", "our team",
    "meet the team", "privacy", "accessibility", "locations", "careers",
)
ANCHOR_BONUS = 10

EXCLUDED_SEGMENTS = frozenset({
    "blog", "news", "tag", "tags", "category", "categories", "product", "products",
    "shop", "cart", "checkout", "account", "login", "wp-admin", "feed", "rss",
    "author", "search",
})

# Never fetched, per spec 3a, regardless of score.
FORBIDDEN_PREFIXES = ("/wp-admin", "/cart", "/checkout", "/account", "/login")

ASSET_EXTENSIONS = frozenset({
    "pdf", "jpg", "jpeg", "png", "gif", "svg", "webp", "bmp", "ico", "avif", "tif", "tiff",
    "zip", "gz", "tar", "rar", "7z", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "rtf",
    "mp4", "mp3", "wav", "mov", "avi", "webm", "css", "js", "json", "xml", "rss", "atom",
    "woff", "woff2", "ttf", "eot", "otf", "csv", "txt", "exe", "dmg", "apk",
})

_YEAR = re.compile(r"^(?:19|20)\d\d$")
_TOKEN_SPLIT = re.compile(r"[-_]+")
_INDEX = re.compile(r"/index\.(?:html?|php|aspx?|jsp|cfm|shtml)$", re.I)
# Whole words only: substring matching scored "roundabout" as "about".
_ANCHOR_RE = re.compile(
    r"\b(?:" + "|".join(k.replace(" ", r"\s+") for k in sorted(
        ("contact", "get in touch", "quote", "estimate", "schedule", "about",
         "our team", "meet the team", "privacy", "accessibility", "locations", "careers"),
        key=len, reverse=True)) + r")\b",
    re.I,
)
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class ScoredUrl:
    url: str            # canonical form, used for dedupe and fetching
    score: int
    tier: str | None
    depth: int
    anchor: str = ""


def canonical(url: str) -> str:
    """One page, one key: https, no www, lowercase path, no index.html, no query."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = _INDEX.sub("/", parts.path or "/").lower()
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, "", ""))


def segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def tokens(segment: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(segment) if t]


def extension_of(path: str) -> str | None:
    segs = segments(path)
    if not segs:
        return None
    last = segs[-1]
    if "." not in last:
        return None
    ext = last.rsplit(".", 1)[1].lower()
    # Only a short alphanumeric run is an extension; "st.-louis" and "dr.-smith" are not.
    return ext if ext.isalnum() and len(ext) <= 5 else None


def is_excluded(path: str) -> bool:
    """Run against the RAW path, before canonicalisation."""
    low = path.lower()
    if any(low.startswith(p) for p in FORBIDDEN_PREFIXES):
        return True
    segs = segments(low)
    for seg in segs:
        if seg in EXCLUDED_SEGMENTS or _YEAR.match(seg):
            return True
    ext = extension_of(low)
    if ext and ext in ASSET_EXTENSIONS:
        return True
    return False


def tier_for(path: str) -> tuple[str | None, int]:
    """Highest matching tier for a path: whole segment, or a hyphen/underscore token."""
    segs = [s.lower() for s in segments(path)]
    seg_set = set(segs)
    token_set: set[str] = set()
    for s in segs:
        token_set.update(tokens(s))
    for name, score, keywords in TIERS:
        if keywords & seg_set or keywords & token_set:
            return name, score
    return None, 0


def anchor_matches(anchor: str) -> bool:
    a = _WS.sub(" ", (anchor or "").lower()).strip()
    return bool(a) and bool(_ANCHOR_RE.search(a))


def score_url(url: str, anchor: str = "") -> ScoredUrl | None:
    """None means never fetch this URL."""
    parts = urlsplit(url)
    raw_path = parts.path or "/"
    if is_excluded(raw_path):
        return None
    tier, base = tier_for(raw_path)
    bonus = ANCHOR_BONUS if anchor_matches(anchor) else 0
    total = base + bonus
    if total <= 0:
        return None
    canon = canonical(url)
    return ScoredUrl(
        url=canon,
        score=total,
        tier=tier,
        depth=len(segments(urlsplit(canon).path)),
        anchor=anchor or "",
    )


def select(
    candidates: list[ScoredUrl], limit: int, *, per_tier_cap: int = 2
) -> list[ScoredUrl]:
    """Deterministic pick of at most `limit` URLs, at most `per_tier_cap` from a tier."""
    best: dict[str, ScoredUrl] = {}
    for c in candidates:
        prev = best.get(c.url)
        if prev is None or c.score > prev.score:
            best[c.url] = c

    ordered = sorted(best.values(), key=lambda c: (-c.score, c.depth, len(c.url), c.url))
    chosen: list[ScoredUrl] = []
    per_tier: dict[str | None, int] = {}
    for c in ordered:
        if len(chosen) >= limit:
            break
        if per_tier.get(c.tier, 0) >= per_tier_cap:
            continue
        per_tier[c.tier] = per_tier.get(c.tier, 0) + 1
        chosen.append(c)
    return chosen


def has_tier(candidates: list[ScoredUrl], tier: str) -> bool:
    return any(c.tier == tier for c in candidates)
