"""The section 11 fixture table, one test per row."""

import gzip
import pathlib

import pytest

from app.pipeline.discovery.robots import parse_sitemaps
from app.pipeline.discovery.sitemap import extract_urls, order_children
from app.pipeline.extract.emails import extract_page
from app.pipeline.filter_rank import rank_candidates
from app.pipeline.parked import check_text

F = pathlib.Path(__file__).parent / "fixtures"
SITE = "acmeroofing.com"
URL = "https://acmeroofing.com/contact"


def load(name: str) -> str:
    return (F / name).read_text(encoding="utf-8")


def emails(name: str) -> dict[str, str]:
    candidates, _text, _title = extract_page(load(name), URL)
    return {c.email: c.method for c in candidates}


def kept(name: str) -> list[str]:
    candidates, _t, _ti = extract_page(load(name), URL)
    return [r.email for r in rank_candidates(candidates, SITE)]


def test_mailto_subject():
    assert emails("mailto_subject.html") == {"mike@acmeroofing.com": "mailto"}


def test_cf_span():
    found = emails("cf_span.html")
    assert found == {"info@acmeroofing.com": "cloudflare"}
    candidates, _t, _ti = extract_page(load("cf_span.html"), URL)
    # "context includes surrounding words"
    assert "office team" in candidates[0].context


def test_cf_link():
    assert emails("cf_link.html") == {"info@acmeroofing.com": "cloudflare"}


def test_obfuscated():
    found = emails("obfuscated.html")
    assert set(found) == {"acmeroof@gmail.com", "jane@acmeroofing.com"}


def test_sentence_yields_no_email():
    """Prose must never become an address (Core Rule 1)."""
    assert emails("sentence.html") == {}


def test_entities():
    found = set(emails("entities.html"))
    assert found == {
        "billing@acmeroofing.com",
        "accounts@acmeroofing.com",
        "press@acmeroofing.com",
        "payroll@acmeroofing.com",
    }


def test_jsonld_graph():
    assert emails("jsonld_graph.html") == {"owner@acmeroofing.com": "jsonld"}


def test_junk_returns_nothing():
    assert kept("junk.html") == []


def test_designer_footer_ranks_own_above_third_party():
    ranked = kept("designer_footer.html")
    assert ranked[0] == "mike@acmeroofing.com"
    assert ranked.index("info@acmeroofing.com") < ranked.index("hello@bluepixel.com")
    assert ranked.index("bluepixel@gmail.com") < ranked.index("hello@bluepixel.com")


def test_parked_page():
    assert check_text(load("parked.html")).parked


def test_robots_two_sitemap_lines():
    assert parse_sitemaps(load("robots.txt"), SITE) == [
        "https://acmeroofing.com/sitemap.xml",
        "https://acmeroofing.com/page-sitemap.xml",
    ]


def test_sitemap_index_prefers_page_and_skips_product():
    urls, is_index = extract_urls(load("sitemap_index.xml"))
    assert is_index
    ordered = order_children(urls)
    assert ordered == ["https://acmeroofing.com/page-sitemap.xml.gz"]


def test_gzipped_page_sitemap():
    text = gzip.decompress((F / "page-sitemap.xml.gz").read_bytes()).decode()
    urls, is_index = extract_urls(text)
    assert not is_index
    assert urls == [
        "https://acmeroofing.com/contact",
        "https://acmeroofing.com/about-us",
        "https://acmeroofing.com/blog/roof-tips",
    ]


@pytest.mark.parametrize(
    "name,absent",
    [
        ("junk.html", "logo@2x.png"),
        ("junk.html", "7f3a9c2b1d4e5f60a1b2@sentry.io"),
        ("junk.html", "you@example.com"),
        ("junk.html", "noreply@acmeroofing.com"),
        ("junk.html", "no_reply@acmeroofing.com"),
        ("junk.html", "wordpress@acmeroofing.com"),
        ("junk.html", "core-js@3.2.1"),
        ("junk.html", "sha256@abc.def"),
        ("junk.html", "react-dom@18.2.0-canary.abc"),
        ("junk.html", "private@acmeroofing.com"),
        ("sentence.html", "online@acmeroofing.com"),
        ("sentence.html", "clients@northwest.com"),
        ("sentence.html", "us@acme.com"),
    ],
)
def test_specific_junk_and_fabrications_never_surface(name, absent):
    assert absent not in kept(name)
