"""Stage 5g contact-form detection.

Correction: the spec's rule -- a form with an email input OR a textarea OR a field named
email/message -- fires on a Mailchimp footer, a blog comment box and a login form, all of
which are on a large share of small-business sites. `contact_form_url` would then be set
almost everywhere and `form_only` would stop distinguishing anything, turning every
email-less domain from `no_contact_info` into `form_only`.

A form now needs two of {email field, textarea, name field} and must not look like a
search, newsletter, login or comment form. The provider-embed list stays a separate
sufficient condition, since those embeds are contact forms by construction.
"""

import re

from selectolax.parser import HTMLParser

PROVIDER_MARKERS = (
    "hsforms", "hbspt.forms", "jotform", "typeform", "gravityforms", "wpforms",
    "formstack", "cognitoforms", "wufoo", "formsite", "formspree", "getform.io",
)

_NOT_CONTACT = re.compile(
    r"(?i)(search|subscribe|signup|sign-up|newsletter|list-manage|mailchimp|login|log-in|"
    r"signin|sign-in|register|comment|respond|wc-|cart|checkout|coupon|password)"
)
_EMAILISH = re.compile(r"(?i)e-?mail")
_MESSAGEISH = re.compile(r"(?i)(message|comments?|details|describe|project|enquiry|inquiry)")
_NAMEISH = re.compile(r"(?i)(^|[^a-z])name([^a-z]|$)|full[-_ ]?name|fname|lname")


def _attrs(node) -> str:
    return " ".join(str(v) for v in node.attributes.values() if v)


def has_contact_form(html: str) -> bool:
    if not html:
        return False
    low = html.lower()
    if any(marker in low for marker in PROVIDER_MARKERS):
        return True
    try:
        tree = HTMLParser(html)
    except Exception:  # noqa: BLE001
        return False

    for form in tree.css("form"):
        blob = _attrs(form)
        if _NOT_CONTACT.search(blob):
            continue

        signals = 0
        has_textarea = bool(form.css("textarea"))
        if has_textarea:
            signals += 1

        field_blob = []
        for field in form.css("input, select"):
            field_blob.append(_attrs(field))
        fields = " ".join(field_blob)

        if _NOT_CONTACT.search(fields) and not has_textarea:
            continue
        if any(
            (f.attributes.get("type") or "").lower() == "email" for f in form.css("input")
        ) or _EMAILISH.search(fields):
            signals += 1
        if _NAMEISH.search(fields):
            signals += 1
        if _MESSAGEISH.search(fields):
            signals += 1

        if signals >= 2:
            return True
    return False


def pick_contact_form_url(pages: list[tuple[str, int, str]]) -> str | None:
    """pages = (url, score, html). Highest-scored page with a form; homepage counts last."""
    best: tuple[int, str] | None = None
    for url, score, html in pages:
        if not has_contact_form(html):
            continue
        if best is None or score > best[0]:
            best = (score, url)
    return best[1] if best else None
