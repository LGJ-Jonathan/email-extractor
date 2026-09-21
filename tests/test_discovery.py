"""Stage 3: robots, sitemaps, links, scoring and selection."""

import pytest

from app.pipeline.discovery import discover_pages
from app.pipeline.discovery.links import extract_links
from app.pipeline.discovery.robots import parse_sitemaps
from app.pipeline.discovery.score import canonical, is_excluded, score_url, select
from app.pipeline.discovery.sitemap import (
    child_is_excluded,
    extract_urls,
    is_sitemap_index,
    maybe_gunzip,
    order_children,
    parse_locs,
)
from app.pipeline.fetch import FetchResult

B = "https://acme.com"


# --- fix 1: exclusions survive trailing-slash normalisation ---------------


@pytest.mark.parametrize(
    "path",
    ["/blog", "/blog/", "/blog/roof-tips", "/news", "/news/", "/category", "/category/roofs",
     "/products", "/products/", "/product/x", "/shop", "/tag/roofing", "/feed",
     "/cart", "/checkout", "/account", "/login", "/wp-admin/admin.php"],
)
def test_index_pages_are_excluded_not_just_subpaths(path):
    """The spec excluded `/blog/` but normalised the trailing slash away first."""
    assert is_excluded(path)
    assert score_url(B + path) is None


# --- fix 2: the year rule is a whole segment -----------------------------


def test_year_segments():
    assert is_excluded("/2024/01/post")
    assert is_excluded("/blog/2024/roof")
    assert not is_excluded("/2024-annual-report")      # spec regex missed this
    assert not is_excluded("/about-1999-founding")


# --- fix 3: asset blocklist, not an extension allowlist ------------------


@pytest.mark.parametrize(
    "path",
    ["/locations/st.-louis", "/about/dr.-smith", "/about-u.s.-operations", "/team/john.smith",
     "/contact.asp", "/about.cfm", "/index.shtml", "/contact.jsp", "/our-team.xhtml",
     "/contact.do", "/about.php5", "/quote.ASPX", "/contact.html", "/contact.php"],
)
def test_legitimate_pages_with_dots_are_kept(path):
    """The allowlist excluded every path with a dot in its last segment."""
    assert not is_excluded(path)


@pytest.mark.parametrize(
    "path", ["/brochure.pdf", "/logo.png", "/style.css", "/app.js", "/data.json",
             "/feed.xml", "/f.woff2", "/video.mp4", "/list.csv", "/archive.zip"],
)
def test_asset_extensions_are_excluded(path):
    assert is_excluded(path)


# --- fix 4: canonical dedupe, determinism, per-tier cap ------------------


def test_canonical_collapses_the_same_page():
    spellings = [
        "http://acme.com/contact", "https://acme.com/contact/", "https://www.acme.com/contact",
        "https://acme.com/contact/index.html", "https://acme.com/Contact",
        "https://acme.com/contact?utm_source=x",
    ]
    assert len({canonical(u) for u in spellings}) == 1


def test_duplicate_spellings_do_not_eat_page_slots():
    cands = [
        score_url("http://acme.com/contact"),
        score_url("https://www.acme.com/contact/"),
        score_url("https://acme.com/contact/index.html"),
        score_url("https://acme.com/about-us"),
        score_url("https://acme.com/request-a-quote"),
    ]
    chosen = select([c for c in cands if c], limit=4)
    assert len(chosen) == 3
    assert len({c.url for c in chosen}) == 3


def test_selection_is_deterministic():
    urls = [f"{B}/contact", f"{B}/contact-us", f"{B}/about", f"{B}/privacy", f"{B}/careers"]
    runs = {
        tuple(c.url for c in select([score_url(u) for u in urls], limit=4))
        for _ in range(20)
    }
    assert len(runs) == 1


def test_per_tier_cap_allows_a_second_contact_page():
    """"At most one per tier" made /contact-us unreachable and forced a legal page in."""
    cands = [score_url(u) for u in
             [f"{B}/contact", f"{B}/contact-us", f"{B}/request-a-quote",
              f"{B}/about-us", f"{B}/privacy"]]
    chosen = [c.url for c in select([c for c in cands if c], limit=4)]
    assert f"{B}/contact-us" in chosen
    assert f"{B}/privacy" not in chosen


# --- spec section 11 "links scoring set" ---------------------------------


def test_spec_scoring_order():
    contact = score_url(f"{B}/contact")
    quote = score_url(f"{B}/request-a-quote")
    about = score_url(f"{B}/about-us")
    privacy = score_url(f"{B}/privacy")
    assert contact.score > quote.score > about.score > privacy.score
    assert (contact.tier, quote.tier, about.tier, privacy.tier) == (
        "contact", "quote", "about", "legal")
    assert score_url(f"{B}/blog/roof-tips") is None
    assert score_url(f"{B}/2024/01/post") is None


def test_anchor_bonus():
    plain = score_url(f"{B}/about-us")
    bonused = score_url(f"{B}/about-us", "Meet the team")
    assert bonused.score == plain.score + 10


def test_untiered_path_with_a_matching_anchor_is_still_fetchable():
    """Resolves a spec ambiguity: /connect, /reach, /hablemos carry no tier keyword, but a
    link labelled "Contact us" is unambiguous. Such a URL scores the bonus alone (10) and
    so ranks below every real tier rather than being dropped."""
    s = score_url(f"{B}/connect", "contact us")
    assert s is not None and s.score == 10 and s.tier is None
    assert score_url(f"{B}/connect") is None          # no anchor, no score


# --- sitemap: the <loc> regex -------------------------------------------


@pytest.mark.parametrize(
    "xml,expected",
    [
        ("<loc>https://a.com/contact</loc>", ["https://a.com/contact"]),
        ("<loc><![CDATA[https://a.com/contact]]></loc>", ["https://a.com/contact"]),
        ("<s:loc>https://a.com/contact</s:loc>", ["https://a.com/contact"]),
        ('<loc xmlns="x">https://a.com/contact</loc>', ["https://a.com/contact"]),
        ("<loc>https://a.com/c?a=1&amp;b=2</loc>", ["https://a.com/c?a=1&b=2"]),
        ("<loc>https://a.com/c?a=1&#38;b=2</loc>", ["https://a.com/c?a=1&b=2"]),
        ("<loc>  https://a.com/contact\n</loc>", ["https://a.com/contact"]),
        ("<loc>https://a.com/a<loc>https://a.com/b</loc>", ["https://a.com/b"]),
        ("<loc>not-a-url</loc>", []),
    ],
)
def test_loc_parsing(xml, expected):
    assert parse_locs(xml) == expected


def test_sitemap_index_detection():
    assert is_sitemap_index('<?xml version="1.0"?><sitemapindex xmlns="x"><sitemap/></sitemapindex>')
    assert is_sitemap_index("<urlset/>" * 0 + "<s:sitemapindex >")
    assert not is_sitemap_index('<?xml version="1.0"?><urlset><url><loc>https://a/</loc></url></urlset>')


def test_gzip_sitemap():
    import gzip
    raw = gzip.compress(b"<urlset><loc>https://a.com/contact</loc></urlset>")
    assert parse_locs(maybe_gunzip(raw)) == ["https://a.com/contact"]


def test_plain_text_sitemap():
    urls, is_index = extract_urls("https://a.com/contact\nhttps://a.com/about\n")
    assert urls == ["https://a.com/contact", "https://a.com/about"]
    assert not is_index


# --- sitemap: the child-filter substring bug ----------------------------


@pytest.mark.parametrize(
    "url",
    ["https://authorityplumbing.com/page-sitemap.xml",
     "https://heritageroofing.com/page-sitemap.xml",
     "https://cottagekitchens.com/page-sitemap.xml",
     "https://vintageautobody.com/page-sitemap.xml",
     "https://advantagehvac.com/page-sitemap.xml",
     "https://stagecoachmoving.com/page-sitemap.xml",
     "https://postalconnections.com/page-sitemap.xml",
     "https://newsomeelectric.com/page-sitemap.xml",
     "https://imagedentistry.com/page-sitemap.xml"],
)
def test_brand_names_no_longer_kill_sitemap_discovery(url):
    """Substring matching over the whole URL excluded these via the HOST."""
    assert not child_is_excluded(url)


@pytest.mark.parametrize(
    "url",
    ["https://acme.com/product-sitemap.xml", "https://acme.com/post-sitemap.xml",
     "https://acme.com/blog-sitemap.xml", "https://acme.com/category-sitemap.xml",
     "https://acme.com/author-sitemap.xml", "https://acme.com/image-sitemap.xml"],
)
def test_real_junk_children_still_excluded(url):
    assert child_is_excluded(url)


def test_children_are_ordered_page_first():
    children = [
        "https://acme.com/product-sitemap.xml",
        "https://acme.com/post-sitemap.xml",
        "https://acme.com/other-sitemap.xml",
        "https://acme.com/page-sitemap.xml",
    ]
    ordered = order_children(children)
    assert ordered[0] == "https://acme.com/page-sitemap.xml"
    assert "https://acme.com/product-sitemap.xml" not in ordered


# --- robots ---------------------------------------------------------------


def test_robots_two_sitemap_lines():
    text = "User-agent: *\nDisallow: /wp-admin/\nSitemap: https://acme.com/sitemap.xml\n" \
           "Sitemap: https://acme.com/page-sitemap.xml\n"
    assert parse_sitemaps(text, "acme.com") == [
        "https://acme.com/sitemap.xml", "https://acme.com/page-sitemap.xml"]


def test_robots_rejects_foreign_and_relative_sitemaps():
    text = "Sitemap: https://evil.com/sitemap.xml\nSitemap: /sitemap.xml\n" \
           "Sitemap: https://www.acme.com/ok.xml\n"
    assert parse_sitemaps(text, "acme.com") == ["https://www.acme.com/ok.xml"]


def test_robots_html_page_yields_nothing():
    assert parse_sitemaps("<!doctype html><html><body>404</body></html>", "acme.com") == []


def test_robots_caps_at_five():
    text = "".join(f"Sitemap: https://acme.com/s{i}.xml\n" for i in range(12))
    assert len(parse_sitemaps(text, "acme.com")) == 5


# --- links ----------------------------------------------------------------


def test_extract_links():
    html = """<html><body>
      <a href="/contact">Contact Us</a>
      <a href="about-us">About</a>
      <a href="https://acme.com/quote#form">Get a Quote</a>
      <a href="mailto:a@acme.com">mail</a>
      <a href="tel:+15551234567">call</a>
      <a href="javascript:void(0)">js</a>
      <a href="https://facebook.com/acme">fb</a>
      <a href="#top">top</a>
    </body></html>"""
    links = extract_links(html, "https://acme.com/")
    urls = [u for u, _ in links]
    assert urls == ["https://acme.com/contact", "https://acme.com/about-us",
                    "https://acme.com/quote"]
    assert links[0][1] == "contact us"


# --- discover_pages -------------------------------------------------------


class TextFetcher:
    def __init__(self, texts: dict[str, str]):
        self.texts = texts
        self.text_calls: list[str] = []

    async def get_html(self, url, *, domain=None):
        raise NotImplementedError

    async def get_text(self, url, *, domain=None) -> FetchResult:
        self.text_calls.append(url)
        if url not in self.texts:
            raise RuntimeError("404")
        return FetchResult(url=url, final_url=url, status=200, html=self.texts[url], bytes=10)


HOME_WITH_BOTH = """<html><body>
  <a href="/contact">Contact</a><a href="/about-us">About us</a>
</body></html>"""


async def test_shortcut_skips_every_sitemap_fetch():
    """Spec 16.2: contact + about from the homepage alone is enough."""
    f = TextFetcher({})
    chosen, diag = await discover_pages(
        f, domain="acme.com", final_url="https://acme.com/",
        homepage_html=HOME_WITH_BOTH, robots_text="Sitemap: https://acme.com/s.xml",
        limit=4,
    )
    assert diag["shortcut"] is True
    assert f.text_calls == []
    assert {c.url for c in chosen} == {"https://acme.com/contact", "https://acme.com/about-us"}


async def test_product_sitemap_listed_first_no_longer_hides_the_contact_page():
    """The spec stopped at the first valid sitemap; a product sitemap buried /contact."""
    f = TextFetcher({
        "https://acme.com/product-sitemap.xml":
            "<urlset>" + "".join(f"<loc>https://acme.com/product/p{i}</loc>" for i in range(50)) + "</urlset>",
        "https://acme.com/page-sitemap.xml":
            "<urlset><loc>https://acme.com/contact</loc><loc>https://acme.com/about-us</loc></urlset>",
    })
    robots = ("Sitemap: https://acme.com/product-sitemap.xml\n"
              "Sitemap: https://acme.com/page-sitemap.xml\n")
    chosen, diag = await discover_pages(
        f, domain="acme.com", final_url="https://acme.com/",
        homepage_html="<html><body>no links</body></html>", robots_text=robots, limit=4,
    )
    urls = {c.url for c in chosen}
    assert "https://acme.com/contact" in urls
    assert diag["shortcut"] is False


async def test_fallback_fires_when_discovery_finds_nothing_useful():
    f = TextFetcher({})
    chosen, diag = await discover_pages(
        f, domain="acme.com", final_url="https://acme.com/",
        homepage_html="<html><body><a href='/blog'>Blog</a></body></html>", limit=4,
    )
    assert diag["fallback"] is True
    assert {c.url for c in chosen} == {
        "https://acme.com/contact", "https://acme.com/contact-us",
        "https://acme.com/about", "https://acme.com/about-us",
    }


async def test_sitemap_index_is_followed_to_its_page_child():
    f = TextFetcher({
        "https://acme.com/sitemap.xml": "",     # unused; seeded as text below
        "https://acme.com/page-sitemap.xml":
            "<urlset><loc>https://acme.com/contact</loc></urlset>",
    })
    index = ('<sitemapindex><sitemap><loc>https://acme.com/product-sitemap.xml</loc></sitemap>'
             '<sitemap><loc>https://acme.com/page-sitemap.xml</loc></sitemap></sitemapindex>')
    chosen, _ = await discover_pages(
        f, domain="acme.com", final_url="https://acme.com/",
        homepage_html="<html></html>", root_sitemap_text=index, limit=4,
    )
    assert "https://acme.com/contact" in {c.url for c in chosen}
    assert "https://acme.com/product-sitemap.xml" not in f.text_calls


async def test_foreign_sitemap_urls_are_ignored():
    f = TextFetcher({})
    chosen, _ = await discover_pages(
        f, domain="acme.com", final_url="https://acme.com/",
        homepage_html="<html></html>",
        root_sitemap_text="<urlset><loc>https://evil.com/contact</loc></urlset>",
        limit=4,
    )
    assert all("evil.com" not in c.url for c in chosen)


# --- anchor matching is whole-word; weak matches must not suppress the fallback ---


def test_anchor_keywords_match_whole_words_only():
    assert score_url(f"{B}/x", "roundabout journey") is None      # not "about"
    assert score_url(f"{B}/x", "rescheduled") is None             # not "schedule"
    assert score_url(f"{B}/x", "bookkeeping") is None
    assert score_url(f"{B}/x", "About us") is not None
    assert score_url(f"{B}/x", "Get in touch") is not None
    assert score_url(f"{B}/x", "Meet   the   team") is not None   # whitespace collapsed


async def test_weak_anchor_only_pages_do_not_suppress_the_fixed_path_fallback():
    """Observed live on nginx.org: two score-10 anchor-only links blocked the fallback,
    so the crawl took a docs page instead of trying /contact."""
    html = ('<html><body><a href="/en">about</a>'
            '<a href="/en/docs/http/ngx_http_core_module.html">predicate locations</a>'
            '</body></html>')
    chosen, diag = await discover_pages(
        TextFetcher({}), domain="acme.com", final_url="https://acme.com/",
        homepage_html=html, limit=4,
    )
    assert diag["fallback"] is True
    urls = [c.url for c in chosen]
    assert urls[0] == "https://acme.com/contact"       # score 100 outranks the score-10 pair
    assert "https://acme.com/en/docs/http/ngx_http_core_module.html" not in urls
