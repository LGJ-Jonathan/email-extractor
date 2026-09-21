"""Stage 5f social links.

Correction: the spec matches network names as substrings of the URL, so `x.com` matched
netflix.com, dropbox.com, box.com, **wix.com** (the footer of every Wix site) and
linux.com -- at 50k domains the `x` column would be mostly junk. Matching is now on the
host. The share/intent filter is also path-anchored: as a bare substring, `share`
dropped `facebook.com/SharedVisionRoofing` and every handle containing the word.
"""

import re
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser

NETWORKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("facebook", ("facebook.com", "fb.com", "fb.me")),
    ("instagram", ("instagram.com",)),
    ("linkedin", ("linkedin.com",)),
    ("x", ("x.com", "twitter.com")),
    ("youtube", ("youtube.com", "youtu.be")),
    ("tiktok", ("tiktok.com",)),
    ("yelp", ("yelp.com",)),
)

_DROP_PATH = re.compile(
    r"(?i)(^/sharer|^/share(?:r|article)?(?:/|$)|/intent/|/dialog/|/plugins/|^/share\.php)"
)
_LINKEDIN_OK = re.compile(r"(?i)/(?:company|in|school|showcase)/")


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def extract_socials(html: str, base_url: str = "") -> dict[str, str]:
    """First URL per network, in document order."""
    if not html:
        return {}
    try:
        tree = HTMLParser(html)
    except Exception:  # noqa: BLE001
        return {}

    out: dict[str, str] = {}
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        parts = urlsplit(href if "//" in href else "https://" + href.lstrip("/"))
        host = (parts.hostname or "").lower()
        path = parts.path or "/"
        if not host or _DROP_PATH.search(path):
            continue
        for name, domains in NETWORKS:
            if name in out or not _host_matches(host, domains):
                continue
            if name == "linkedin" and not _LINKEDIN_OK.search(path):
                continue
            out[name] = href if "//" in href else f"https://{host}{path}"
            break
    return out
