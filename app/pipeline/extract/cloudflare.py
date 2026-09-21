"""Stage 5a Cloudflare email-protection decoding.

`decode_cf` itself is the spec's, verified correct by round-trip. The corrections are
around it:

* **Odd-length hex.** The spec's `[0-9a-fA-F]{6,}` accepts an odd run and `decode_cf`
  then appends a spurious character with no exception, so `try/except` cannot catch it.
  The pattern now requires whole bytes.
* **No output validation.** `decode_cf("414142434445")` yields control characters, which
  would flow into `context` and then into the TypeSafe request body. The result must now
  fullmatch the email pattern.
* **Element replacement was undefined for void and nested tags.** "the tag through its
  closing tag" has no referent for `<input data-cfemail=... />`, and a greedy reading
  swallows the rest of the document; a nested same-name child left an orphaned `</span>`
  for the parser. The decoded address is now injected as text immediately after the
  carrying tag, which is well defined for every shape and still gives real context.
* **method="cloudflare" was unreachable.** The spec rewrites to plain text and `mailto:`,
  which Stage 5c then re-derives as `text`/`mailto` -- so the section 11 fixture
  asserting `method cloudflare` could never pass. Decoded addresses are returned
  separately so the method can be set at the source.
"""

import re

from app.pipeline.extract.patterns import EMAIL_FULL

CF_ATTR = re.compile(r'data-cfemail\s*=\s*"((?:[0-9a-fA-F]{2}){5,})"', re.I)
CF_LINK = re.compile(r'/cdn-cgi/l/email-protection#((?:[0-9a-fA-F]{2}){5,})', re.I)
_CARRIER = re.compile(r'<[^<>]*data-cfemail\s*=\s*"((?:[0-9a-fA-F]{2}){5,})"[^<>]*>', re.I)


def decode_cf(hexstr: str) -> str | None:
    """Spec's XOR decode, with whole-byte input and a validated result."""
    try:
        key = int(hexstr[:2], 16)
        out = "".join(chr(int(hexstr[i : i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except (ValueError, IndexError):
        return None
    out = out.strip()
    return out.lower() if EMAIL_FULL.fullmatch(out) else None


def decode_html(html: str) -> tuple[str, set[str]]:
    """Return (html with addresses made visible, the set of decoded addresses)."""
    if not html or "cfemail" not in html.lower() and "email-protection" not in html.lower():
        return html, set()

    found: set[str] = set()

    def carrier(m: re.Match[str]) -> str:
        email = decode_cf(m.group(1))
        if not email:
            return m.group(0)
        found.add(email)
        # Inject as text just inside the element: well defined for void and nested tags.
        return f"{m.group(0)} {email} "

    out = _CARRIER.sub(carrier, html)

    def link(m: re.Match[str]) -> str:
        email = decode_cf(m.group(1))
        if not email:
            return m.group(0)
        found.add(email)
        return f"mailto:{email}"

    out = CF_LINK.sub(link, out)
    return out, found
