"""Stage 5b HTML normalisation, applied after Cloudflare decoding.

Three corrections, all of which caused lost or fabricated emails:

1. **Rule 1's "other `\\u00XX` -> space" is dropped.** Applied literally it destroys the
   very escapes the rule exists to convert (`\\u0040` becomes a space before it can become
   `@`) and blanks accented text. Only `\\u0040` and `\\u002e` are converted.
2. **JSON-LD bodies are excluded from normalisation.** `json.loads` already decodes
   `\\u0040` correctly; normalising first mangled the JSON so the parse failed and the
   most reliable email source on the page was lost.
3. **Rule 6 requires a contact cue.** As written, `<word> at <word> dot <tld>` rewrites
   ordinary marketing copy: "Order online at acmeroofing dot com" became
   `online@acmeroofing.com`, which is own-domain and non-role, so it ranked 1 and beat
   the site's real `info@`. That address never appeared on the page -- a Core Rule 1
   violation. It now fires only within 40 characters after a contact cue, which is where
   real obfuscation lives. The bracketed forms of rule 5 are untouched and still catch
   `acmeroof [at] gmail [dot] com`.

Rule 3's global `%20 -> space` is also dropped: it rewrote href paths and JSON-LD URL
values. Percent-decoding happens in the mailto extractor instead, where it belongs.
"""

import re

from app.pipeline.htmlscan import iter_blocks

_JSONLD_TYPE = re.compile(r"type\s*=\s*[\"']?application/ld\+json", re.I)

_U_AT = re.compile(r"\\u0040|\\x40", re.I)
_U_DOT = re.compile(r"\\u002e|\\x2e", re.I)

_ENT_AT = re.compile(r"&#0*64;|&#x0*40;|&commat;", re.I)
_ENT_DOT = re.compile(r"&#0*46;|&#x0*2e;|&period;", re.I)

_PCT_AT = re.compile(r"%40", re.I)

_ZERO_WIDTH = re.compile(r"[​‌‍﻿­⁠]")

_BRACKET_AT = re.compile(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", re.I)
_BRACKET_DOT = re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.I)

# Rule 6, cue-gated. The cue may sit up to 40 characters before the local part.
# Whole words: without the boundary, "mail" matched inside "Gmail" and "Mailchimp", so
# "Sign up with Gmail. Find us at acmeroofing dot com" produced us@acmeroofing.com.
_CUE = r"\b(?:e-?mail|mail|contact|reach\s+us|write\s+to|enquir(?:y|ies)|inquir(?:y|ies))\b"
_SPELLED = re.compile(
    rf"({_CUE}[^<>]{{0,40}}?\b)"
    r"([a-z0-9._%+\-]+)\s+at\s+([a-z0-9-]+)\s+dot\s+(com|net|org|us|co|biz|info|io)\b",
    re.I,
)


def _normalise_fragment(text: str) -> str:
    text = _U_AT.sub("@", text)
    text = _U_DOT.sub(".", text)
    text = _ENT_AT.sub("@", text)
    text = _ENT_DOT.sub(".", text)
    text = _PCT_AT.sub("@", text)
    text = _ZERO_WIDTH.sub("", text)
    text = _BRACKET_AT.sub("@", text)
    text = _BRACKET_DOT.sub(".", text)
    text = _SPELLED.sub(lambda m: f"{m.group(1)}{m.group(2)}@{m.group(3)}.{m.group(4)}", text)
    return text


def normalize_html(html: str) -> str:
    """Normalise everything except JSON-LD script bodies, which are left byte-exact."""
    if not html:
        return ""
    parts: list[str] = []
    last = 0
    for b in iter_blocks(html, "script"):
        if not _JSONLD_TYPE.search(b.attrs):
            continue
        parts.append(_normalise_fragment(html[last:b.start]))
        parts.append(html[b.start:b.end])          # JSON-LD untouched
        last = b.end
    parts.append(_normalise_fragment(html[last:]))
    return "".join(parts)
