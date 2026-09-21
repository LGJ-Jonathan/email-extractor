"""Stage 7: TypeSafe judgment (spec section 6, Stage 7).

Corrections to the spec's decision logic, each of which emitted a wrong address:

1. **The `else` branch shipped the email TypeSafe had just rejected.** When the pick's
   `belongs` is below the threshold the spec falls back to `rules_best` -- which on a
   one-candidate domain *is* the rejected address, emitted with `business_emails` empty.
   That is the designer-email leak the run measured at 14.3%, arriving through the
   default path. `rules_best` is now only used when it independently passes `belongs`.
2. **`belongs[pick.choice]` could raise KeyError before the safety net.** A returned
   choice differing in case or whitespace crashed the domain; the membership check ran
   after the lookup. Now normalised, looked up with `.get`, and checked first.
3. **Duplicate candidates collapsed the criteria object** while `belongs_i` stayed keyed
   by index, so answers were attributed to the wrong address. Candidates are deduped
   before the body is built.
4. **`confidence` kept the model's number while reporting `best_email_source="rules"`**
   for a different address. It is now null whenever the source is rules.
5. **The prompt was uncapped** -- with the Stage 6 cap of 50 a single request could carry
   52 questions and 50 context windows. Capped at the top 10 ranked candidates.
"""

import asyncio
import logging
import random

import httpx

from app.pipeline.filter_rank import RankedEmail
from app.ratelimit import CircuitBreaker, CircuitOpen
from app.provider_keys import current as current_key
from app.settings import settings

log = logging.getLogger("email_extractor.typesafe")

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MAX_JUDGE_CANDIDATES = 10
RETRY_DELAYS = (2.0, 4.0, 8.0, 16.0, 32.0)
TIMEOUT_S = 30.0

BEST_CONTACT_Q = (
    "Which email is the best way to reach the owner or a decision maker at the business "
    "that owns {domain}? Prefer a named person at the business over a generic inbox. "
    "Emails for the web designer, hosting platform, software vendor, or other third "
    "parties do not count."
)
SITE_STATUS_Q = "What kind of website is {domain}?"
BELONGS_Q = (
    "Does the email {email} belong to the business that owns {domain} (the owner, an "
    "employee, or a company inbox), rather than a web designer, platform, vendor, or "
    "other third party?"
)


class Judgment:
    """What Stage 7 decided, in the shape DomainResult needs."""

    def __init__(
        self,
        best_email: str | None = None,
        best_email_source: str | None = None,
        confidence: float | None = None,
        needs_review: bool = False,
        business_emails: list[str] | None = None,
        site_status: str | None = None,
        typesafe_called: bool = False,
        typesafe_error: bool = False,
        said_none: bool = False,
        belongs: dict[str, float] | None = None,
    ) -> None:
        self.best_email = best_email
        self.best_email_source = best_email_source
        self.confidence = confidence
        self.needs_review = needs_review
        self.business_emails = business_emails or []
        self.site_status = site_status
        self.typesafe_called = typesafe_called
        self.typesafe_error = typesafe_error
        self.said_none = said_none
        self.belongs = belongs or {}


def should_call(ranked: list[RankedEmail]) -> bool:
    """Spec: 2+ candidates, or any candidate at rank 3 or 4."""
    if not ranked:
        return False
    return len(ranked) >= 2 or any(r.rank >= 3 for r in ranked)


def rules_only(ranked: list[RankedEmail]) -> Judgment:
    """The spec's "TypeSafe skipped" branch."""
    if not ranked:
        return Judgment()
    return Judgment(
        best_email=ranked[0].email,
        best_email_source="rules",
        confidence=None,
        needs_review=False,
        business_emails=[r.email for r in ranked if r.rank in (1, 2)],
    )


def build_body(ranked: list[RankedEmail], domain: str, page_title: str) -> tuple[dict, list[str]]:
    """Returns (request body, the deduped email list the answers map onto)."""
    seen: set[str] = set()
    picked: list[RankedEmail] = []
    for r in ranked:
        key = r.email.lower()
        if key in seen:
            continue
        seen.add(key)
        picked.append(r)
        if len(picked) >= MAX_JUDGE_CANDIDATES:
            break

    emails = [r.email.lower() for r in picked]
    criteria: dict[str, str | None] = {e: None for e in emails}
    criteria["none"] = "None of these emails belongs to this business."

    questions: dict[str, dict] = {
        "best_contact": {
            "type": "choice",
            "instructions": BEST_CONTACT_Q.format(domain=domain),
            "criteria": criteria,
        },
        "site_status": {
            "type": "choice",
            "instructions": SITE_STATUS_Q.format(domain=domain),
            "criteria": {
                "operating_business": (
                    "A real business that is currently operating and selling products "
                    "or services."
                ),
                "parked_or_for_sale": (
                    "A parked domain, placeholder, or domain listed for sale."
                ),
                "other": (
                    "Something else, such as a personal blog, directory, or closed business."
                ),
            },
        },
    }
    for i, r in enumerate(picked):
        questions[f"belongs_{i}"] = {
            "type": "noul",
            "instructions": BELONGS_Q.format(email=r.email, domain=domain),
        }

    body = {
        "model": settings.typesafe_model,
        "state": {
            "business_domain": domain,
            "page_title": page_title[:150],
            "candidates": [
                {
                    "email": r.email,
                    "context": r.candidate.context[:400],
                    "source_url": r.candidate.source_urls[0] if r.candidate.source_urls else "",
                }
                for r in picked
            ],
        },
        "questions": questions,
    }
    return body, emails


def decide(answers: dict, emails: list[str], ranked: list[RankedEmail]) -> Judgment:
    """The spec's decision block, with the fallback and lookup corrected."""
    best = answers.get("best_contact") or {}
    raw_choice = (best.get("choice") or "").strip()
    choice = raw_choice.lower()
    confidence = float(best.get("confidence") or 0.0)

    belongs: dict[str, float] = {}
    for i, email in enumerate(emails):
        node = answers.get(f"belongs_{i}") or {}
        try:
            belongs[email] = float(node.get("noul"))
        except (TypeError, ValueError):
            belongs[email] = 0.0

    site = (answers.get("site_status") or {}).get("choice")
    site_conf = float((answers.get("site_status") or {}).get("confidence") or 0.0)
    business = [e for e in emails if belongs.get(e, 0.0) >= settings.typesafe_belongs_min]

    # Membership is checked BEFORE the belongs lookup, so an unexpected choice string
    # can never raise.
    valid_pick = choice in emails
    accepted = (
        valid_pick
        and confidence >= settings.typesafe_confidence_min
        and belongs.get(choice, 0.0) >= settings.typesafe_belongs_min
    )

    common = {
        "business_emails": business,
        "site_status": site if site_conf >= 0.8 else site,
        "typesafe_called": True,
        "belongs": belongs,
    }

    if accepted:
        return Judgment(
            best_email=choice, best_email_source="typesafe", confidence=confidence,
            needs_review=False, **common,
        )

    if choice == "none" and confidence >= settings.typesafe_confidence_min:
        return Judgment(
            best_email=None, best_email_source="typesafe", confidence=confidence,
            needs_review=False, said_none=True, **common,
        )

    # Fall back to rules -- but never to an address TypeSafe judged as not belonging.
    fallback = ranked[0].email.lower() if ranked else None
    if fallback is not None and belongs.get(fallback, 1.0) < settings.typesafe_belongs_min:
        fallback = next((e for e in business), None)
    return Judgment(
        best_email=fallback,
        best_email_source="rules" if fallback else "typesafe",
        confidence=None,                       # never the model's number for a rules pick
        needs_review=True,
        **common,
    )


class TypeSafeClient:
    def __init__(self, breaker: CircuitBreaker | None = None) -> None:
        self._sem = asyncio.Semaphore(settings.typesafe_concurrency)
        self.breaker = breaker or CircuitBreaker("typesafe")

    async def judge(
        self, ranked: list[RankedEmail], domain: str, page_title: str = ""
    ) -> Judgment:
        if not should_call(ranked):
            return rules_only(ranked)

        body, emails = build_body(ranked, domain, page_title)
        try:
            answers = await self._post(body)
        except Exception as e:  # noqa: BLE001 - spec: any failure falls back to rules
            log.warning("typesafe_failed", extra={"domain": domain, "err": type(e).__name__})
            j = rules_only(ranked)
            j.typesafe_error = True
            return j
        return decide(answers, emails, ranked)

    async def _post(self, body: dict) -> dict:
        from app.pipeline.fetch import get_client

        last: Exception | None = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            self.breaker.check()
            try:
                async with self._sem:
                    r = await get_client().post(
                        ENDPOINT,
                        json=body,
                        timeout=TIMEOUT_S,
                        headers={
                            "Authorization": f"Bearer {current_key('typesafe')}",
                            "Content-Type": "application/json",
                        },
                    )
                if r.status_code in (429, 529) or 500 <= r.status_code < 600:
                    self.breaker.record(False)
                    last = RuntimeError(f"typesafe HTTP {r.status_code}")
                elif r.status_code >= 400:
                    self.breaker.record(r.status_code not in (401, 402, 403))
                    raise RuntimeError(f"typesafe HTTP {r.status_code}")
                else:
                    self.breaker.record(True)
                    return r.json().get("answers") or {}
            except CircuitOpen:
                raise
            except RuntimeError:
                raise
            except Exception as e:  # noqa: BLE001
                self.breaker.record(False)
                last = e
            if attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt] * (1.0 + random.random() * 0.5)
                await asyncio.sleep(delay)
        raise last or RuntimeError("typesafe exhausted")
