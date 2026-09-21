"""Section 7 status assignment. Exactly one status per domain, first match wins."""

from dataclasses import dataclass

STATUSES = (
    "invalid_input",
    "fetch_failed",
    "parked_or_for_sale",
    "no_business_email",
    "found",
    "form_only",
    "no_contact_info",
)


@dataclass
class StatusInputs:
    invalid_input: bool = False
    fetch_failed: bool = False
    parked: bool = False
    typesafe_said_none: bool = False     # "none" at or above TYPESAFE_CONFIDENCE_MIN
    best_email: str | None = None
    business_emails: list[str] | None = None
    contact_form_url: str | None = None


def assign_status(s: StatusInputs) -> str:
    """The section 7 table, in order."""
    if s.invalid_input:
        return "invalid_input"
    if s.fetch_failed:
        return "fetch_failed"
    # Two corrections to the table's first-match-wins order:
    #   * a parked verdict must not bury a real address. TypeSafe calling a live site
    #     "parked" while we hold a valid own-domain email shipped that email under a
    #     status every downstream filter discards.
    #   * "no business email" must not be returned while business_emails is populated.
    if s.parked and not s.best_email:
        return "parked_or_for_sale"
    if s.typesafe_said_none and not (s.business_emails or s.best_email):
        return "no_business_email"
    if s.best_email:
        return "found"
    if s.contact_form_url:
        return "form_only"
    return "no_contact_info"
