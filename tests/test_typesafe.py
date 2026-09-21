"""Stage 7 decision logic, including the branches that emitted a rejected address."""

import pytest

from app.pipeline.extract.emails import EmailCandidate
from app.pipeline.filter_rank import rank_candidates
from app.pipeline.typesafe import (
    MAX_JUDGE_CANDIDATES,
    build_body,
    decide,
    rules_only,
    should_call,
)

SITE = "acmeroofing.com"


def ranked(*emails, method="mailto"):
    cands = [
        EmailCandidate(email=e, method=method, source_urls=[f"https://{SITE}/"], order=i)
        for i, e in enumerate(emails)
    ]
    return rank_candidates(cands, SITE)


def answers(choice, conf, belongs, site="operating_business", site_conf=0.95):
    a = {
        "best_contact": {"choice": choice, "confidence": conf},
        "site_status": {"choice": site, "confidence": site_conf},
    }
    for i, b in enumerate(belongs):
        a[f"belongs_{i}"] = {"noul": b}
    return a


# --- when to call ---------------------------------------------------------


def test_should_call_matrix():
    assert not should_call([])
    assert not should_call(ranked("mike@acmeroofing.com"))          # single rank 1
    assert not should_call(ranked("info@acmeroofing.com"))          # single rank 2
    assert should_call(ranked("mike@acmeroofing.com", "info@acmeroofing.com"))
    assert should_call(ranked("bob@gmail.com"))                     # rank 3 alone
    assert should_call(ranked("hello@bluepixel.com"))               # rank 4 alone


# --- the branch that leaked -----------------------------------------------


def test_rejected_pick_is_never_emitted_as_a_rules_fallback():
    """The spec's else branch returned rules_best, which on a one-candidate domain IS
    the address TypeSafe just rejected -- with business_emails empty."""
    r = ranked("hello@bluepixel.com")
    j = decide(answers("hello@bluepixel.com", 0.91, [0.10]), ["hello@bluepixel.com"], r)
    assert j.best_email is None
    assert j.business_emails == []
    assert j.needs_review is True


def test_low_confidence_none_does_not_resurrect_the_rejected_pick():
    r = ranked("hello@bluepixel.com")
    j = decide(answers("none", 0.70, [0.08]), ["hello@bluepixel.com"], r)
    assert j.best_email is None
    assert j.said_none is False          # below threshold, so not no_business_email


def test_fallback_prefers_a_belonging_candidate_over_the_rejected_one():
    r = ranked("hello@bluepixel.com", "info@acmeroofing.com")
    emails = [x.email for x in r]
    belongs = [0.95 if e.endswith("acmeroofing.com") else 0.05 for e in emails]
    j = decide(answers("nonsense@x.com", 0.4, belongs), emails, r)
    assert j.best_email == "info@acmeroofing.com"
    assert j.needs_review is True


def test_invariant_best_email_is_always_in_business_emails():
    for choice, conf, b in [
        ("mike@acmeroofing.com", 0.99, [0.97, 0.9]),
        ("mike@acmeroofing.com", 0.5, [0.97, 0.9]),
        ("none", 0.99, [0.02, 0.01]),
        ("weird", 0.9, [0.97, 0.9]),
    ]:
        r = ranked("mike@acmeroofing.com", "info@acmeroofing.com")
        j = decide(answers(choice, conf, b), [x.email for x in r], r)
        if j.best_email:
            assert j.best_email in j.business_emails, (choice, conf, b)


# --- lookup and confidence safety ----------------------------------------


def test_unexpected_choice_string_does_not_raise():
    r = ranked("mike@acmeroofing.com", "info@acmeroofing.com")
    emails = [x.email for x in r]
    for bad in ["Mike@AcmeRoofing.com ", "", "not-an-email", "MIKE@ACMEROOFING.COM"]:
        j = decide(answers(bad, 0.95, [0.9, 0.9]), emails, r)
        assert j is not None


def test_case_mismatched_choice_still_accepted():
    r = ranked("mike@acmeroofing.com", "info@acmeroofing.com")
    j = decide(answers("MIKE@ACMEROOFING.COM", 0.95, [0.9, 0.9]), [x.email for x in r], r)
    assert j.best_email == "mike@acmeroofing.com"
    assert j.best_email_source == "typesafe"


def test_confidence_is_null_whenever_the_source_is_rules():
    r = ranked("mike@acmeroofing.com", "info@acmeroofing.com")
    j = decide(answers("mike@acmeroofing.com", 0.40, [0.9, 0.9]), [x.email for x in r], r)
    assert j.best_email_source == "rules"
    assert j.confidence is None


def test_missing_belongs_answer_is_treated_as_zero():
    r = ranked("mike@acmeroofing.com", "info@acmeroofing.com")
    a = answers("mike@acmeroofing.com", 0.99, [])
    j = decide(a, [x.email for x in r], r)
    assert j.best_email != "mike@acmeroofing.com" or j.best_email is None


# --- request shape --------------------------------------------------------


def test_duplicates_are_deduped_so_answers_map_correctly():
    cands = [
        EmailCandidate(email="info@acmeroofing.com", method="text", source_urls=["u"], order=0),
        EmailCandidate(email="info@acmeroofing.com", method="text", source_urls=["u"], order=1),
        EmailCandidate(email="mike@acmeroofing.com", method="text", source_urls=["u"], order=2),
    ]
    body, emails = build_body(rank_candidates(cands, SITE), SITE, "t")
    belongs_qs = [k for k in body["questions"] if k.startswith("belongs_")]
    criteria = body["questions"]["best_contact"]["criteria"]
    assert len(emails) == len(belongs_qs) == len(criteria) - 1   # minus "none"


def test_judge_prompt_is_capped():
    many = [f"person{i}@acmeroofing.com" for i in range(40)]
    body, emails = build_body(ranked(*many), SITE, "t")
    assert len(emails) == MAX_JUDGE_CANDIDATES
    assert len(body["state"]["candidates"]) == MAX_JUDGE_CANDIDATES


def test_body_matches_the_spec_shape():
    r = ranked("mike@acmeroofing.com", "hello@bluepixel.com")
    body, _ = build_body(r, SITE, "Acme Roofing")
    assert body["model"]
    assert set(body["state"]) == {"business_domain", "page_title", "candidates"}
    assert "none" in body["questions"]["best_contact"]["criteria"]
    assert body["questions"]["site_status"]["type"] == "choice"
    assert body["questions"]["belongs_0"]["type"] == "noul"


# --- skipped branch -------------------------------------------------------


def test_rules_only_matches_the_spec_skip_branch():
    r = ranked("mike@acmeroofing.com", "info@acmeroofing.com")
    j = rules_only(r)
    assert j.best_email == "mike@acmeroofing.com"
    assert j.best_email_source == "rules"
    assert j.confidence is None
    assert j.needs_review is False
    assert j.business_emails == ["mike@acmeroofing.com", "info@acmeroofing.com"]
    assert j.typesafe_called is False


def test_rules_only_on_no_candidates():
    j = rules_only([])
    assert j.best_email is None and j.business_emails == []
