"""Stage 5c JSON-LD source: walk every block, including @graph, for `email` values."""

import json
import re

from app.pipeline.extract.patterns import EMAIL, clean

JSONLD_BLOCK = re.compile(
    r"<script[^>]*type\s*=\s*[\"']?application/ld\+json[^>]*>(.*?)</script\s*>",
    re.I | re.S,
)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def _walk(node: object, out: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() == "email":
                if isinstance(value, str):
                    out.append(value)
                elif isinstance(value, list):
                    out.extend(v for v in value if isinstance(v, str))
            else:
                _walk(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk(item, out)


def blocks(html: str) -> list[str]:
    return [b for b in JSONLD_BLOCK.findall(html or "")]


def emails_from_block(body: str) -> tuple[list[str], bool]:
    """Return (emails, parsed_ok). On a parse failure the caller falls back to regex."""
    raw: list[str] = []
    for text in (body, _TRAILING_COMMA.sub(r"\1", body)):
        try:
            _walk(json.loads(text), raw)
        except (ValueError, RecursionError):
            continue
        break
    else:
        return [], False

    out: list[str] = []
    for value in raw:
        value = value.strip()
        if value.lower().startswith("mailto:"):
            value = value[7:]
        value = clean(value.split("?")[0])
        if value:
            out.append(value)
    return out, True


def extract(html: str) -> tuple[list[str], list[str]]:
    """(jsonld emails, bodies that failed to parse and need the regex fallback)."""
    good: list[str] = []
    failed: list[str] = []
    for body in blocks(html):
        emails, ok = emails_from_block(body)
        if ok:
            good.extend(emails)
        else:
            failed.append(body)
    return good, failed


def regex_emails(text: str) -> list[str]:
    return [clean(m) for m in EMAIL.findall(text or "")]
