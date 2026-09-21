"""Row preparation and output shaping (spec 5, 9, 14). No database required."""

import csv
import io
import json

import pytest

from app.ingest import detect_column, parse_bytes
from app.jobs import (
    EX_PREFIX,
    RESULT_FIELDS,
    csv_line,
    json_line,
    output_header,
    output_row,
    prepare_items,
    prepare_plain,
    result_cells,
    unique_domains,
)
from app.schemas import DomainResult, EmailCandidateOut

FILE = (
    "Company Name,Company Website,City\n"
    "Acme Roofing,https://www.acme.com/contact,Dallas\n"
    "Bob HVAC,acme.com,Austin\n"
    "Nameless,,Houston\n"
    "Lone Star,Lone Star Electrical LLC,Waco\n"
    "Social Co,facebook.com/socialco,Plano\n"
)


def parsed():
    return parse_bytes(FILE.encode("utf-8"), "leads.csv")


def test_prepare_items_preserves_order_and_original_cells():
    p = parsed()
    items = prepare_items(p, detect_column(p))
    assert [i.row_index for i in items] == [0, 1, 2, 3, 4]
    assert items[0].raw_row["Company Name"] == "Acme Roofing"
    assert items[0].raw_row["City"] == "Dallas"
    assert items[0].domain == "acme.com"
    assert items[1].domain == "acme.com"          # same domain, different spelling
    assert items[2].status == "invalid_input" and items[2].reason == "empty"
    assert items[3].reason == "not_a_url"
    assert items[4].reason == "social_or_directory"


def test_unique_domains_dedupes_and_keeps_input_order():
    p = parsed()
    items = prepare_items(p, detect_column(p))
    assert unique_domains(items) == ["acme.com"]


def test_unique_domains_orders_by_first_appearance():
    items = prepare_plain(["b.com", "a.com", "b.com", "c.com"])
    assert unique_domains(items) == ["b.com", "a.com", "c.com"]


def test_prepare_plain_for_the_json_body_form():
    items = prepare_plain(["acme.com", "N/A", "facebook.com/x"])
    assert items[0].domain == "acme.com"
    assert items[1].reason == "empty"
    assert items[2].reason == "social_or_directory"
    assert items[0].raw_row == {"website": "acme.com"}


# --- output shape ---------------------------------------------------------


def a_result() -> DomainResult:
    return DomainResult(
        domain="acme.com",
        final_domain="acme.com",
        status="found",
        best_email="mike@acme.com",
        best_email_source="typesafe",
        confidence=0.93,
        needs_review=False,
        business_emails=["mike@acme.com", "info@acme.com"],
        all_emails=[
            EmailCandidateOut(email="mike@acme.com", source_urls=["https://acme.com/"],
                              method="mailto", rank=1, belongs=0.97),
            EmailCandidateOut(email="info@acme.com", source_urls=["https://acme.com/"],
                              method="text", rank=2, belongs=0.9),
        ],
        phones=["+12123794444"],
        socials={"facebook": "https://facebook.com/acme"},
        contact_form_url="https://acme.com/contact",
        mx_provider="google",
        pages_fetched=["https://acme.com/", "https://acme.com/contact"],
        jina_tokens=1234,
    )


def test_header_is_source_columns_then_ex_prefixed():
    header = output_header(["Company Name", "Company Website", "City"])
    assert header[:3] == ["Company Name", "Company Website", "City"]
    assert header[3] == EX_PREFIX + RESULT_FIELDS[0]
    assert all(h.startswith(EX_PREFIX) for h in header[3:])
    assert len(header) == 3 + len(RESULT_FIELDS)


def test_output_row_replays_every_original_column():
    cols = ["Company Name", "Company Website", "City"]
    raw = {"Company Name": "Acme Roofing", "Company Website": "acme.com", "City": "Dallas"}
    row = output_row(cols, raw, a_result())
    assert row[:3] == ["Acme Roofing", "acme.com", "Dallas"]
    assert len(row) == len(output_header(cols))
    header = output_header(cols)
    assert row[header.index("ex_best_email")] == "mike@acme.com"
    assert row[header.index("ex_all_emails")] == "mike@acme.com, info@acme.com"
    assert row[header.index("ex_facebook")] == "https://facebook.com/acme"
    assert row[header.index("ex_mx_provider")] == "google"


def test_unfinished_rows_are_pending_but_keep_their_domain():
    cols = ["Company Website"]
    header = output_header(cols)
    row = output_row(cols, {"Company Website": "acme.com"}, None, pending_domain="acme.com")
    assert row[header.index("ex_status")] == "pending"
    assert row[header.index("ex_domain")] == "acme.com"
    assert row[header.index("ex_best_email")] == ""


def test_retrying_flag():
    cells = result_cells(None, pending_domain="acme.com", retrying=True)
    assert cells["status"] == "pending" and cells["retrying"] == "true"


def test_every_row_has_the_same_width_as_the_header():
    cols = ["Company Name", "Company Website", "City"]
    width = len(output_header(cols))
    for result, kw in ((a_result(), {}), (None, {"pending_domain": "acme.com"}), (None, {})):
        assert len(output_row(cols, {}, result, **kw)) == width


def test_csv_round_trips_hostile_cells():
    cols = ["notes"]
    raw = {"notes": 'he said "hi", then\nleft'}
    line = csv_line(output_row(cols, raw, a_result()))
    back = next(csv.reader(io.StringIO(line)))
    assert back[0] == 'he said "hi", then\nleft'
    assert len(back) == len(output_header(cols))


def test_formula_injection_is_neutralised_in_source_and_result_cells():
    cols = ["Company Website"]
    row = output_row(cols, {"Company Website": "=cmd|'/C calc'!A1"}, a_result())
    assert row[0].startswith("'=")
    for cell in row:
        assert not cell.startswith(("=", "+", "@", "\t", "\r"))


def test_json_line_shape():
    cols = ["Company Name", "Company Website"]
    raw = {"Company Name": "Acme", "Company Website": "acme.com"}
    payload = json.loads(json_line(cols, raw, a_result()))
    assert payload["Company Name"] == "Acme"
    assert payload["ex_best_email"] == "mike@acme.com"
    assert payload["ex_status"] == "found"


def test_one_output_row_per_input_row_including_invalid_ones():
    p = parsed()
    items = prepare_items(p, detect_column(p))
    cols = p.headers
    rows = [
        output_row(cols, it.raw_row, None, pending_domain=it.domain) for it in items
    ]
    assert len(rows) == len(p.rows) == 5
    header = output_header(cols)
    assert rows[2][header.index("ex_domain")] == ""      # the blank-website row is kept


def test_result_fields_cover_every_spec_8_column():
    for field in ("status", "best_email", "confidence", "needs_review", "business_emails",
                  "all_emails", "phones", "contact_form_url", "mx_provider",
                  "site_status", "pages_fetched", "error_reason", "final_domain"):
        assert field in RESULT_FIELDS
