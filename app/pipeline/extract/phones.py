"""Stage 5e phone numbers.

Correction: `PhoneNumberMatcher(text, "US")` at default leniency turns any bare ten-digit
run into a phone number -- verified on "License #2015551234", "Order ID 8005550199" and
"EIN 20-5551234, DUNS 800 555 0199", all of which appear on real contractor sites. A
match now needs either a `tel:` href or a phone cue within 40 characters before it.

Note for section 11 fixtures: `555-01xx` numbers are *invalid* NANP and are rejected by
`is_valid_number`, so a fixture using them asserts an empty list and passes vacuously.
Use a genuinely valid number such as +1 212 379 4444.
"""

import re

import phonenumbers
from selectolax.parser import HTMLParser

MAX_PHONES = 5
CUE = re.compile(r"(?i)(tel|telephone|phone|call|text|fax|mobile|cell|toll[- ]?free|☎|📞)")
CUE_WINDOW = 40


def _e164(raw: str, region: str = "US") -> str | None:
    try:
        number = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def from_tel_links(html: str) -> list[str]:
    if not html:
        return []
    try:
        tree = HTMLParser(html)
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if href[:4].lower() == "tel:":
            e164 = _e164(href[4:].strip())
            if e164 and e164 not in out:
                out.append(e164)
    return out


def from_text(text: str) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for match in phonenumbers.PhoneNumberMatcher(
        text, "US", leniency=phonenumbers.Leniency.VALID
    ):
        before = text[max(0, match.start - CUE_WINDOW) : match.start]
        if not CUE.search(before):
            continue
        e164 = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
        if e164 not in out:
            out.append(e164)
    return out


def extract_phones(html: str, text: str) -> list[str]:
    """tel: links first, then cued text matches, capped."""
    out = from_tel_links(html)
    for phone in from_text(text):
        if phone not in out:
            out.append(phone)
    return out[:MAX_PHONES]
