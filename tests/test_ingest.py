"""Section 14 file parsing and column detection."""

import io

import pytest

from app.ingest import (
    IngestError,
    decode,
    detect_column,
    normalize_column,
    parse_bytes,
    preview,
    score_column,
    sniff_delimiter,
)


def csv_bytes(text: str, encoding: str = "utf-8") -> bytes:
    return text.encode(encoding)


# --- encoding -------------------------------------------------------------


def test_utf8_bom_is_stripped():
    p = parse_bytes("﻿website,city\nacme.com,Dallas\n".encode("utf-8"), "x.csv")
    assert p.headers == ["website", "city"]


def test_utf16_with_bom():
    raw = "website,city\nacme.com,Dallas\n".encode("utf-16")
    text, enc = decode(raw)
    assert "website" in text and enc.startswith("utf-16")


def test_cp1252_fallback():
    raw = "website,name\nacme.com,Caf\xe9 Roofing\n".encode("cp1252")
    p = parse_bytes(raw, "x.csv")
    assert p.rows[0]["name"] == "Café Roofing"


# --- delimiters and headers ----------------------------------------------


@pytest.mark.parametrize("delim", [",", ";", "\t", "|"])
def test_delimiter_sniffing(delim):
    text = delim.join(["website", "city"]) + "\n" + delim.join(["acme.com", "Dallas"]) + "\n"
    p = parse_bytes(csv_bytes(text), "x.csv")
    assert p.headers == ["website", "city"]
    assert p.rows[0]["website"] == "acme.com"


def test_tsv_extension_forces_tab():
    assert sniff_delimiter("a\tb\n1\t2\n", "x.tsv") == "\t"


def test_headerless_file_gets_generated_columns():
    p = parse_bytes(csv_bytes("acme.com,bobshvac.com\nfoo.com,bar.com\n"), "x.csv")
    assert p.had_header is False
    assert p.headers == ["column_1", "column_2"]
    assert len(p.rows) == 2


def test_header_row_detected_when_any_cell_is_not_a_domain():
    p = parse_bytes(csv_bytes("website,city\nacme.com,Dallas\n"), "x.csv")
    assert p.had_header is True


def test_quoted_fields_with_embedded_newlines_and_commas():
    text = 'website,notes\nacme.com,"line one\nline two, with comma"\nbob.com,plain\n'
    p = parse_bytes(csv_bytes(text), "x.csv")
    assert len(p.rows) == 2
    assert p.rows[0]["notes"] == "line one\nline two, with comma"
    assert p.rows[1]["website"] == "bob.com"


def test_ragged_rows_do_not_crash():
    p = parse_bytes(csv_bytes("a,b,c\n1,2\n1,2,3,4\n"), "x.csv")
    assert p.rows[0]["c"] == ""
    assert len(p.rows) == 2


def test_original_order_and_all_columns_preserved():
    text = "website,city,rank\nb.com,B,2\na.com,A,1\n"
    p = parse_bytes(csv_bytes(text), "x.csv")
    assert [r["website"] for r in p.rows] == ["b.com", "a.com"]
    assert set(p.rows[0]) == {"website", "city", "rank"}


# --- column detection -----------------------------------------------------


@pytest.mark.parametrize(
    "header", ["website", "Website", "WEBSITE URL", "Company Website", "company_domain",
               "domain", "URL", "Homepage", "Business Website"],
)
def test_header_match_is_high_confidence(header):
    p = parse_bytes(csv_bytes(f"name,{header}\nAcme,acme.com\n"), "x.csv")
    g = detect_column(p)
    assert g.column == header
    assert (g.confidence, g.method) == ("high", "header")


def test_header_priority_prefers_website_over_domain():
    p = parse_bytes(csv_bytes("domain,website\na.com,b.com\n"), "x.csv")
    assert detect_column(p).column == "website"


def test_content_scoring_when_no_header_matches():
    rows = "\n".join(f"Acme {i},acme{i}.com,Dallas" for i in range(10))
    p = parse_bytes(csv_bytes(f"company,link,city\n{rows}\n"), "x.csv")
    g = detect_column(p)
    assert g.column == "link"
    assert (g.confidence, g.method) == ("medium", "content")


def test_content_scoring_ignores_social_heavy_columns():
    rows = "\n".join(
        f"facebook.com/biz{i},acme{i}.com" for i in range(10)
    )
    p = parse_bytes(csv_bytes(f"social,link\n{rows}\n"), "x.csv")
    assert detect_column(p).column == "link"


def test_email_fallback_when_no_website_column():
    rows = "\n".join(f"Acme {i},owner{i}@acme{i}.com" for i in range(10))
    p = parse_bytes(csv_bytes(f"company,contact email\n{rows}\n"), "x.csv")
    g = detect_column(p)
    assert g.column is None
    assert g.method == "email"
    assert g.email_column == "contact email"


def test_nothing_found_returns_null_column():
    rows = "\n".join(f"Acme {i},Dallas,{i}" for i in range(10))
    p = parse_bytes(csv_bytes(f"company,city,rank\n{rows}\n"), "x.csv")
    g = detect_column(p)
    assert g.column is None and g.email_column is None
    assert g.confidence == "none"


def test_score_column():
    assert score_column(["acme.com", "bob.com", "foo.com"]) == 1.0
    assert score_column(["Acme LLC", "N/A", "-"]) == 0.0
    assert 0.0 < score_column(["acme.com", "Acme LLC"]) < 1.0
    assert score_column([]) == 0.0


# --- deriving domains from emails ----------------------------------------


def test_normalize_column_derives_from_email_and_skips_free_providers():
    text = ("email\nowner@acmeroofing.com\nbob@gmail.com\ndispatch@lonestar.net\n")
    p = parse_bytes(csv_bytes(text), "x.csv")
    out = normalize_column(p, "email", derive_from_email=True)
    assert out[0].domain == "acmeroofing.com"
    assert out[1].reason == "free_email_only"
    assert out[2].domain == "lonestar.net"


# --- preview payload ------------------------------------------------------


def test_preview_payload():
    text = ("website,city\n"
            "https://www.acme.com/contact,Dallas\n"
            "acme.com,Dallas\n"
            "Acme Roofing LLC,Dallas\n"
            ",Dallas\n"
            "facebook.com/acme,Dallas\n")
    p = parse_bytes(csv_bytes(text), "x.csv")
    pv = preview(p, detect_column(p))
    assert pv["detected_column"] == "website"
    assert pv["row_count"] == 5
    assert pv["unique_domains"] == 1          # the two acme spellings collapse
    assert pv["duplicate_domains"] == 1
    assert pv["empty_website_count"] == 1
    assert pv["invalid_count"] == 3
    assert len(pv["samples"]) == 5
    assert pv["columns"] == ["website", "city"]


# --- limits ---------------------------------------------------------------


def test_row_cap():
    rows = "\n".join(f"acme{i}.com" for i in range(50_001))
    with pytest.raises(IngestError, match="limit is 50,000"):
        parse_bytes(csv_bytes(f"website\n{rows}\n"), "x.csv")


def test_byte_cap():
    with pytest.raises(IngestError, match="limit is 50 MB"):
        parse_bytes(b"x" * (51 * 1024 * 1024), "x.csv")


def test_empty_file():
    with pytest.raises(IngestError):
        parse_bytes(b"", "x.csv")


# --- xlsx -----------------------------------------------------------------


def test_xlsx_round_trip():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Company Website", "City"])
    ws.append(["https://acme.com", "Dallas"])
    ws.append(["bobshvac.com", "Austin"])
    buf = io.BytesIO()
    wb.save(buf)
    p = parse_bytes(buf.getvalue(), "leads.xlsx")
    assert p.headers == ["Company Website", "City"]
    assert len(p.rows) == 2
    g = detect_column(p)
    assert g.column == "Company Website" and g.confidence == "high"


def test_derive_fallback_never_resurrects_a_free_provider():
    """The fallback re-parsed the bare domain of a rejected email, turning
    bob@gmail.com into gmail.com as a crawl target."""
    text = "email\nbob@gmail.com\nowner@yahoo.com\ninfo@acme.com\n"
    p = parse_bytes(csv_bytes(text), "x.csv")
    out = normalize_column(p, "email", derive_from_email=True)
    assert [n.domain for n in out] == [None, None, "acme.com"]
    assert out[0].reason == out[1].reason == "free_email_only"
