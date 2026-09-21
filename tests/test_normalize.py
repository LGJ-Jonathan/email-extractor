"""Section 15 input cases, one test per row of the table."""

import pytest

from app.pipeline.normalize import (
    SITE_BUILDER_SUFFIXES,
    _extract,
    crawl_url,
    normalize_input,
    registrable,
)


@pytest.mark.parametrize("raw", ["", "   ", "N/A", "n/a", "none", "-", "null", "#N/A", "TBD"])
def test_blank_and_null_tokens(raw):
    r = normalize_input(raw)
    assert (r.status, r.reason) == ("invalid_input", "empty")


@pytest.mark.parametrize("raw", ["Acme Roofing LLC", "Lone Star Electrical", "n/a see notes"])
def test_company_name_is_not_a_url(raw):
    r = normalize_input(raw)
    assert (r.status, r.reason) == ("invalid_input", "not_a_url")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Acme (acmeroofing.com)", "acmeroofing.com"),
        ("Acme Roofing - acmeroofing.com", "acmeroofing.com"),
        ("see www.bobshvac.com!", "bobshvac.com"),
        ("<https://acme.com>", "acme.com"),
    ],
)
def test_url_inside_text(raw, expected):
    assert normalize_input(raw).domain == expected


def test_email_in_website_column_uses_its_domain():
    r = normalize_input("info@acmeroofing.com")
    assert r.domain == "acmeroofing.com"
    assert r.domain_source == "email"


@pytest.mark.parametrize("raw", ["bobshvac@gmail.com", "owner@yahoo.com", "a@protonmail.com"])
def test_free_provider_email_is_rejected(raw):
    r = normalize_input(raw)
    assert (r.status, r.reason) == ("invalid_input", "free_email_only")


@pytest.mark.parametrize("raw", ["a.com, b.com", "a.com / b.com", "a.com;b.com", "a.com|b.com"])
def test_multiple_values_takes_the_first_and_notes_it(raw):
    r = normalize_input(raw)
    assert r.domain == "a.com"
    assert "multiple_values" in r.notes


@pytest.mark.parametrize(
    "raw",
    [
        "facebook.com/PatriotFencingTX",
        "https://www.instagram.com/acme",
        "linkedin.com/company/acme",
        "https://www.yelp.com/biz/acme",
        "https://www.google.com/maps/place/Acme",
        "https://g.page/acme",
        "business.site/acme",
        "linktr.ee/acme",
        "x.com/acme",
        "youtube.com/@acme",
        "angi.com/companylist/us/tx/acme.htm",
        "thumbtack.com/tx/acme",
        "bbb.org/us/tx/acme",
        "nextdoor.com/pages/acme",
    ],
)
def test_social_and_directory_urls_rejected(raw):
    r = normalize_input(raw)
    assert (r.status, r.reason) == ("invalid_input", "social_or_directory")


# --- the platform-collapse fix -------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("bobshvac.squarespace.com", "bobshvac.squarespace.com"),
        ("acme.godaddysites.com", "acme.godaddysites.com"),
        ("acme.weebly.com", "acme.weebly.com"),
        ("acmeroofing.myshopify.com", "acmeroofing.myshopify.com"),
        ("joe.blogspot.com", "joe.blogspot.com"),
        ("acme.square.site", "acme.square.site"),
        ("acme.webflow.io", "acme.webflow.io"),
        ("acme.carrd.co", "acme.carrd.co"),
        ("acme.editorx.io", "acme.editorx.io"),
        ("acme.wordpress.com", "acme.wordpress.com"),
        ("https://www.acme.weebly.com/contact", "acme.weebly.com"),
    ],
)
def test_site_builder_hosts_never_collapse_to_the_platform_root(raw, expected):
    assert normalize_input(raw).domain == expected


def test_wixsite_keeps_the_first_path_segment():
    assert normalize_input("joesdiner.wixsite.com/menu").domain == "joesdiner.wixsite.com/menu"
    assert normalize_input("https://joesdiner.wixsite.com/menu/about").domain == (
        "joesdiner.wixsite.com/menu"
    )


@pytest.mark.parametrize("suffix", SITE_BUILDER_SUFFIXES)
def test_bare_platform_root_is_not_a_business_site(suffix):
    r = normalize_input(suffix)
    assert r.status == "invalid_input"


def test_psl_private_domains_alone_would_not_have_fixed_this():
    """Justifies SITE_BUILDER_SUFFIXES existing on top of include_psl_private_domains."""
    still_collapsing = {
        "bobshvac.squarespace.com": "squarespace.com",
        "acme.godaddysites.com": "godaddysites.com",
        "acme.weebly.com": "weebly.com",
    }
    for host, collapsed in still_collapsing.items():
        assert _extract(host).registered_domain == collapsed   # tldextract alone: wrong
        assert registrable(host) == host                       # our key: right


def test_distinct_platform_sites_do_not_share_a_crawl_key():
    keys = {normalize_input(f"biz{i}.squarespace.com").domain for i in range(50)}
    assert len(keys) == 50


# --- normalisation details ------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("HTTPS://WWW.AcmeRoofing.COM/", "acmeroofing.com"),
        ("  acmeroofing.com  ", "acmeroofing.com"),
        ("www2.acme.com", "acme.com"),
        ("acme.com:8080/x?y=1", "acme.com"),
        ("acme.com.", "acme.com"),
        ("https://acme.co.uk/contact", "acme.co.uk"),
        ("https://shop.acme.com/x", "acme.com"),
    ],
)
def test_normalisation(raw, expected):
    assert normalize_input(raw).domain == expected


def test_idn_becomes_punycode_but_display_keeps_the_original():
    r = normalize_input("münchen-dach.de")
    assert r.domain == "xn--mnchen-dach-thb.de"
    assert r.display == "münchen-dach.de"


@pytest.mark.parametrize("raw", ["192.168.1.1", "http://10.0.0.1/", "8.8.8.8"])
def test_ip_addresses_rejected(raw):
    r = normalize_input(raw)
    assert (r.status, r.reason) == ("invalid_input", "not_a_url")


def test_same_domain_many_spellings_yields_one_key():
    spellings = [
        "acmeroofing.com", "www.acmeroofing.com", "https://acmeroofing.com",
        "https://www.acmeroofing.com/", "HTTP://AcmeRoofing.com/contact?x=1",
        "  acmeroofing.com/  ",
    ]
    assert len({normalize_input(s).domain for s in spellings}) == 1


def test_crawl_url_round_trips_including_wixsite_paths():
    assert crawl_url("acme.com") == "https://acme.com/"
    assert crawl_url("acme.com", www=True) == "https://www.acme.com/"
    assert crawl_url("acme.com", scheme="http") == "http://acme.com/"
    assert crawl_url("joesdiner.wixsite.com/menu") == "https://joesdiner.wixsite.com/menu/"


def test_never_raises_on_hostile_input():
    for raw in ["\x00", "@@@", "http://", "://x", "a" * 5000, "..", ".", "..." , "//acme.com",
                "http://[::1]/", "acme..com", "-acme.com", "acme-.com"]:
        normalize_input(raw)   # must not raise
