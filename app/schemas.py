"""Request/response models and the DomainResult output record (spec sections 5 and 8)."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

# Exactly the seven statuses of spec section 7. "pending" is deliberately absent: it is a
# CSV-only literal for rows whose domain has not finished, never a persisted DomainResult.
Status = Literal[
    "invalid_input",
    "fetch_failed",
    "parked_or_for_sale",
    "no_business_email",
    "found",
    "form_only",
    "no_contact_info",
]

ExtractionMethod = Literal["mailto", "jsonld", "cloudflare", "text", "attribute", "script"]
MxProvider = Literal["google", "microsoft", "other", "none"]
SocialNetwork = Literal["facebook", "instagram", "linkedin", "x", "youtube", "tiktok", "yelp"]

SOCIAL_NETWORKS: tuple[str, ...] = (
    "facebook",
    "instagram",
    "linkedin",
    "x",
    "youtube",
    "tiktok",
    "yelp",
)


class EmailCandidateOut(BaseModel):
    """One surviving candidate, as exposed in DomainResult.all_emails."""

    email: str
    source_urls: list[str] = Field(default_factory=list)
    method: ExtractionMethod
    rank: int
    belongs: float | None = None


class DomainResult(BaseModel):
    domain: str
    final_domain: str | None = None
    status: Status
    best_email: str | None = None
    best_email_source: Literal["typesafe", "rules"] | None = None
    confidence: float | None = None
    needs_review: bool = False
    business_emails: list[str] = Field(default_factory=list)
    all_emails: list[EmailCandidateOut] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    socials: dict[str, str] = Field(default_factory=dict)
    contact_form_url: str | None = None
    mx_provider: MxProvider | None = None
    site_status: str | None = None
    pages_fetched: list[str] = Field(default_factory=list)
    jina_tokens: int = 0
    typesafe_called: bool = False
    typesafe_error: bool = False
    error_reason: str | None = None
    processed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# --- API request/response -------------------------------------------------

MAX_ITEMS_PER_JOB = 50_000


class ExtractRequest(BaseModel):
    url: str
    fresh: bool = False


class CreateJobRequest(BaseModel):
    items: list[str] = Field(default_factory=list)
    webhook_url: str | None = None
    fresh: bool = False


class CreateJobResponse(BaseModel):
    job_id: str
    total: int
    unique_domains: int
    cached: int


class JobStatusResponse(BaseModel):
    status: str
    total: int
    done: int
    failed: int
    unique_domains: int
    hit_rate_so_far: float
    typesafe_calls: int
    jina_tokens: int


# --- CSV ------------------------------------------------------------------

CSV_COLUMNS: tuple[str, ...] = (
    "row_index",
    "input_value",
    "domain",
    "final_domain",
    "status",
    "best_email",
    "best_email_source",
    "confidence",
    "needs_review",
    "business_emails",
    "all_emails",
    "phones",
    "contact_form_url",
    "facebook",
    "instagram",
    "linkedin",
    "x",
    "youtube",
    "tiktok",
    "yelp",
    "mx_provider",
    "site_status",
    "pages_fetched",
    "error_reason",
)


def _join(values: list[str]) -> str:
    return ", ".join(values)


# Excel/Sheets execute a cell that starts with any of these. Values reaching the CSV are
# attacker-controlled (they come from the uploaded file and from fetched pages), so every
# field is neutralised on the way out.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: str) -> str:
    return "'" + value if value.startswith(_FORMULA_PREFIXES) else value


def to_csv_row(
    row_index: int,
    input_value: str,
    result: DomainResult | None,
    domain: str | None = None,
) -> list[str]:
    """One results.csv row. `result=None` means the domain has not finished yet.

    `domain` is the job_item's normalized domain, which is known even while the row is
    pending — without it every in-flight row would ship a blank domain column.
    """
    if result is None:
        row = {
            "row_index": str(row_index),
            "input_value": input_value,
            "domain": domain or "",
            "status": "pending",
        }
        return [csv_safe(row.get(col, "")) for col in CSV_COLUMNS]

    row: dict[str, str] = {
        "row_index": str(row_index),
        "input_value": input_value,
        "domain": result.domain,
        "final_domain": result.final_domain or "",
        "status": result.status,
        "best_email": result.best_email or "",
        "best_email_source": result.best_email_source or "",
        "confidence": "" if result.confidence is None else f"{result.confidence:.3f}",
        "needs_review": "true" if result.needs_review else "false",
        "business_emails": _join(result.business_emails),
        "all_emails": _join([c.email for c in result.all_emails]),
        "phones": _join(result.phones),
        "contact_form_url": result.contact_form_url or "",
        "mx_provider": result.mx_provider or "",
        "site_status": result.site_status or "",
        "pages_fetched": _join(result.pages_fetched),
        "error_reason": result.error_reason or "",
    }
    for network in SOCIAL_NETWORKS:
        row[network] = result.socials.get(network, "")
    return [csv_safe(row.get(col, "")) for col in CSV_COLUMNS]
