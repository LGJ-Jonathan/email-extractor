# SPEC: Website Email Extractor

Build instructions for a service that takes business websites and returns the contact emails published on those sites, ranked, with a status per domain. Written to be handed directly to Claude Code. Follow it exactly; where a value is marked TUNE, keep the default until the eval harness says otherwise.

---

## 0. Scope

**In scope**
- Input: website URLs or domains of US businesses, up to 50,000 per job.
- Scrape only the business's own website (same registrable domain).
- Return one record per domain: best email, all business emails, phones, socials, contact form URL, status, confidence.

**Out of scope (do not build)**
- JavaScript/browser rendering of any kind.
- Any off-site lookup: search engines, Google Business Profile, Facebook, directories.
- SMTP mailbox probing.
- Following links to PDFs, vCards, or other files.
- Person-level email guessing (first.last@ patterns).

**Core rules**
1. Code finds every email. The model (TypeSafe) only chooses among emails that were found verbatim. The app must never output an email that did not appear on a fetched page.
2. Raw HTML never leaves the app except to Jina (fetch) and never goes to TypeSafe. TypeSafe gets short text snippets only.
3. Every domain ends with exactly one status from section 7.

---

## 1. Stack

| Concern | Choice |
| --- | --- |
| Language | Python 3.12 |
| API | FastAPI + Uvicorn |
| Queue | Redis + arq |
| Database | Postgres 16, SQLAlchemy 2 (async) + Alembic |
| HTTP client | httpx (async, HTTP/2 off) |
| HTML parsing | selectolax |
| Sitemaps | own parser in `discovery/sitemap.py` (do not add ultimate-sitemap-parser; GPL) |
| Domain parsing | tldextract, with the bundled suffix list only (no network fetch: `tldextract.TLDExtract(suffix_list_urls=())`) |
| Phones | phonenumbers |
| DNS | dnspython (async resolver) |
| Settings | pydantic-settings |
| Tests | pytest, pytest-asyncio, respx (mock httpx) |
| Deploy target | Docker Compose on the home server (Thor): api, worker, redis, postgres |

---

## 2. Repo layout

```
email-extractor/
  app/
    main.py                 # FastAPI app, routes
    settings.py             # pydantic-settings
    models.py               # SQLAlchemy tables
    schemas.py              # Pydantic request/response + DomainResult
    worker.py               # arq worker settings + job functions
    pipeline/
      run.py                # process_domain(): orchestrates stages 1-7
      normalize.py          # stage 1
      dns_check.py          # stage 1
      fetch.py              # Fetcher interface, JinaFetcher, HttpxFetcher
      discovery/
        robots.py
        sitemap.py
        links.py
        score.py            # stage 2 selection
      extract/
        normalize_html.py
        emails.py
        cloudflare.py
        jsonld.py
        phones.py
        socials.py
        forms.py
        context.py
      filter_rank.py        # stage 5
      typesafe.py           # stage 6
      parked.py
      status.py
    ratelimit.py            # token buckets
  migrations/
  scripts/
    eval.py                 # eval harness (section 11)
  tests/
    fixtures/               # HTML/XML fixtures (section 11)
    test_*.py
  docker-compose.yml
  Dockerfile
  .env.example
  README.md
```

---

## 3. Settings (`.env.example`)

```
API_KEY=change-me                 # required header X-API-Key for all routes
DATABASE_URL=postgresql+asyncpg://app:app@postgres:5432/extractor
REDIS_URL=redis://redis:6379/0

FETCH_BACKEND=jina                # jina | httpx
JINA_API_KEY=
JINA_RPM=500                      # TUNE to the Jina plan's real limit
JINA_TIMEOUT_S=10

TYPESAFE_API_KEY=
TYPESAFE_MODEL=jev-latest
TYPESAFE_CONCURRENCY=10
TYPESAFE_CONFIDENCE_MIN=0.8       # TUNE
TYPESAFE_BELONGS_MIN=0.5          # TUNE

GLOBAL_FETCH_CONCURRENCY=50       # TUNE
PER_DOMAIN_CONCURRENCY=2
MAX_PAGES_PER_DOMAIN=5            # homepage + 4
MAX_PAGE_BYTES=2000000
CACHE_DAYS=90
USER_AGENT=Mozilla/5.0 (compatible; ContactFinder/1.0)
```

Never commit real keys. Never log key values.

---

## 4. Data model (Postgres)

**jobs**
- `id` uuid pk
- `created_at`, `finished_at` timestamptz
- `status` enum: queued, running, done, failed
- `total`, `done_count`, `failed_count` int
- `webhook_url` text null

**job_items** (one per input row)
- `id` bigserial pk
- `job_id` fk jobs
- `row_index` int
- `input_value` text (as submitted)
- `domain` text (normalized root domain; null if unparseable)
- index on (job_id, row_index)

**domains** (one per unique root domain; this is the cache and checkpoint)
- `domain` text pk
- `stage` enum: pending, dns, discovery, fetching, extracting, judging, done
- `status` text null (section 7)
- `result` jsonb null (the DomainResult, section 8)
- `attempts` int default 0
- `updated_at`, `finished_at` timestamptz

**pages** (debug and audit; keep 30 days)
- `id` bigserial pk
- `domain` fk domains
- `url` text, `http_status` int, `bytes` int, `jina_tokens` int null, `score` int, `fetched_at`

Raw HTML is not stored in Postgres. Optional: write it gzipped to `/data/html/{domain}/{sha1(url)}.html.gz` when `DEBUG_STORE_HTML=true`.

---

## 5. API

All routes require header `X-API-Key: $API_KEY`; return 401 otherwise.

**POST /jobs**
- Body JSON: `{"items": ["acme.com", "https://www.foo.com/contact", ...], "webhook_url": "https://..."}` (webhook optional)
- Or multipart upload: a CSV file with a column named `website` (case-insensitive; also accept `domain`, `url`).
- Max 50,000 items per job; 413 if more.
- Creates job + job_items, normalizes domains, dedupes, enqueues one arq task per unique domain that is not cached (section 9). Returns `{"job_id": "...", "total": N, "unique_domains": M, "cached": K}`.

**GET /jobs/{id}**
- Returns `{"status", "total", "done", "failed", "unique_domains", "hit_rate_so_far", "typesafe_calls", "jina_tokens"}`.

**GET /jobs/{id}/results.csv**
- Streams one row per job_item, in `row_index` order, joined to the domain result. Rows whose domain is unfinished are included with `status=pending`. Columns: section 8 CSV columns.

**GET /jobs/{id}/results.json**
- Same data as JSON lines.

**POST /extract**
- Body `{"url": "acme.com"}`. Runs the pipeline synchronously for one domain (bypasses queue, respects cache unless `"fresh": true`). Returns DomainResult. For testing and small Clay use.

**Webhook**
- If `webhook_url` set, POST each finished DomainResult (plus `job_id`, `row_indexes`) as JSON. Retry 3 times with backoff 2s, 8s, 30s. Webhook failure never fails the domain.

---

## 6. Pipeline

`pipeline/run.py: async def process_domain(domain: str) -> DomainResult`

Stages run in order. After each stage, update `domains.stage`. On worker crash, arq retries the task (max 3 attempts); the task resumes from scratch for that domain (stages are cheap to redo; only the finished result is checkpointed).

### Stage 1: Normalize and DNS

`normalize.py`
1. Trim, lowercase. If no scheme, prepend `http://` for parsing only.
2. Parse host. Strip leading `www.`.
3. Root domain = `tldextract` registered domain (`example.co.uk` style handled). If empty or IP address, the job_item gets `domain = null` and status `invalid_input`.

`dns_check.py`
1. Resolve A/AAAA for `domain` and `www.domain` (timeout 5s). If neither resolves: status `fetch_failed`, reason `dns`. Stop.
2. Resolve MX for `domain`. Store `mx_provider`:
   - any MX host ending in `google.com` or `googlemail.com` -> `google`
   - ending in `outlook.com` or containing `protection.outlook` -> `microsoft`
   - other MX present -> `other`
   - none -> `none`
3. Record NS hosts for parked detection.

### Stage 2: Fetch homepage

1. Fetch `https://{domain}/`. If it fails (error or status >= 400), try `https://www.{domain}/`, then `http://{domain}/`.
2. All fail: status `fetch_failed`, reason `homepage`. Stop.
3. Record `final_url` and `final_domain` (root domain of the final URL after redirects). If `final_domain != domain`, store both and use `final_domain` for own-domain matching and for all further fetches.

### Stage 3: Discover pages

**3a. robots.txt** (`discovery/robots.py`)
- Fetch `https://{final_domain}/robots.txt` as text. Ignore failures.
- Extract lines matching `(?im)^\s*sitemap:\s*(\S+)` -> sitemap URLs. Keep only same-root-domain URLs. Max 5.
- Do not enforce Disallow rules for these few public pages, but never fetch paths under `/wp-admin`, `/cart`, `/checkout`, `/account`, `/login`.

**3b. Sitemaps** (`discovery/sitemap.py`)
- Candidates in order: robots sitemaps, then `/sitemap.xml`, `/sitemap_index.xml`, `/wp-sitemap.xml`, `/page-sitemap.xml`. Stop trying candidates once one returns valid XML with at least one `<loc>`.
- Handle `.xml.gz` (gunzip) and plain-text sitemaps (one URL per line).
- If the document is a `<sitemapindex>`: collect child `<loc>` URLs, sort so children whose URL contains `page` come first, then others; exclude children containing `product`, `post`, `blog`, `news`, `image`, `video`, `category`, `tag`, `author`. Fetch up to 3 children. Recursion depth max 2.
- Collect page `<loc>` URLs, same root domain only, max 5,000 URLs total. Parse with a tolerant regex `<loc>\s*(.*?)\s*</loc>` (sitemaps are often malformed XML); unescape `&amp;`.

**3c. Homepage links** (`discovery/links.py`)
- From homepage HTML, collect every `<a href>`: resolve relative URLs, drop fragments, drop `mailto:`/`tel:`/`javascript:`, keep same root domain only. Store (url, anchor_text lowercased and whitespace-collapsed).

**3d. Score and select** (`discovery/score.py`)

Merge 3b and 3c into one URL set (normalize: lowercase host, strip trailing slash, drop query string except keep none). Score each URL by its path and its anchor text (if known). A URL's score is the highest matching tier score, plus 10 if the anchor text also matches any tier keyword.

| Tier | Score | Keywords (match as whole path segment or hyphen/underscore-separated token, case-insensitive) |
| --- | --- | --- |
| Contact | 100 | contact, contact-us, contactus, get-in-touch, reach-us |
| Quote | 80 | quote, get-a-quote, request-a-quote, free-estimate, estimate, schedule, book, booking, appointment |
| About/Team | 60 | about, about-us, our-story, team, our-team, leadership, staff, meet-the-team, company, who-we-are |
| Legal | 40 | privacy, privacy-policy, accessibility, terms, terms-of-service, legal |
| Locations/Careers | 20 | locations, location, offices, careers, jobs |

Anchor text keywords for the same tiers: "contact", "get in touch", "quote", "estimate", "schedule", "about", "our team", "meet the team", "privacy", "accessibility", "locations", "careers".

Exclusions (score = 0, never fetched): paths containing `/blog/`, `/news/`, `/tag/`, `/category/`, `/product/`, `/products/`, `/shop/`, `/cart`, `/checkout`, `/account`, `/login`, `/wp-admin`, `/feed`, a 4-digit year segment (`/20\d\d/`), or file extensions other than none/.html/.htm/.php/.aspx.

Selection:
- Sort by score desc, then by path depth asc, then path length asc.
- Take at most one URL per tier first (highest tier first), then fill remaining slots by score.
- Total fetched pages = homepage + up to `MAX_PAGES_PER_DOMAIN - 1` = 4 more. Only URLs with score > 0.
- If no sitemap and no scored links: also try fixed paths `/contact`, `/contact-us`, `/about`, `/about-us` (count toward the 4). Skip 404s silently.

### Stage 4: Fetch pages (`fetch.py`)

Define an interface:
```python
class FetchResult(BaseModel):
    url: str; final_url: str; status: int; html: str; bytes: int; jina_tokens: int | None

class Fetcher(Protocol):
    async def get_html(self, url: str) -> FetchResult: ...
    async def get_text(self, url: str) -> FetchResult: ...   # robots, sitemaps
```

**JinaFetcher** (default)
- `GET https://r.jina.ai/{url}`
- Headers for `get_html`:
  ```
  Authorization: Bearer {JINA_API_KEY}
  Accept: application/json
  X-Engine: direct
  X-Respond-With: html
  X-Retain-Images: none
  X-Timeout: 10
  X-Remove-Selector: style, svg, img, iframe, noscript, link, meta, .swiper, .slick-slider, .owl-carousel, script:not([type="application/ld+json"])
  ```
- Headers for `get_text`: same auth/accept, `X-Engine: direct`, `X-Respond-With: text`, no selectors.
- Parse JSON response: HTML/text is in `data.content`; final URL in `data.url`; tokens in `data.usage.tokens` if present (store null otherwise).
- Milestone 1 check: confirm the returned content is raw HTML (contains `<a ` and `href=`). If Jina returns markdown instead, switch the header to `X-Return-Format: html` and re-verify. Also confirm `script:not(...)` in X-Remove-Selector keeps JSON-LD blocks; if not, remove `script...` from the selector list entirely and strip non-JSON-LD scripts in code.

**HttpxFetcher** (drop-in alternative, `FETCH_BACKEND=httpx`)
- Plain GET with `USER_AGENT`, follow redirects (max 5), timeout 10s, stream and abort past `MAX_PAGE_BYTES`, skip unless `content-type` contains `html` (for get_html) or `text`/`xml` (for get_text). Decode with response encoding, fallback utf-8 with `errors="replace"`.
- In code, strip `<script>` blocks except `type="application/ld+json"`, plus `<style>` and `<svg>`, before extraction.

**Both fetchers**
- Rate limiting: global token bucket at `JINA_RPM` (Jina only), global semaphore `GLOBAL_FETCH_CONCURRENCY`, per-domain semaphore `PER_DOMAIN_CONCURRENCY`.
- Retries: on timeout, connection error, 429, 500-599: retry up to 2 times with backoff 1s then 4s (honor `Retry-After` on 429, capped at 30s).
- Circuit breaker: if >30% of Jina calls in the last 200 fail, pause all fetch workers for 60s and log `jina_circuit_open`.
- A page that fails after retries is recorded in `pages` and skipped; only a failed homepage fails the domain.

### Stage 5: Extract (`extract/`)

Run on every fetched page. Every candidate carries: `email`, `source_url`, `method`, `context`.

**5a. Cloudflare decode first** (`cloudflare.py`)
```python
def decode_cf(hexstr: str) -> str:
    key = int(hexstr[:2], 16)
    return "".join(chr(int(hexstr[i:i+2], 16) ^ key) for i in range(2, len(hexstr), 2))
```
- Find `data-cfemail="([0-9a-fA-F]{6,})"` and `/cdn-cgi/l/email-protection#([0-9a-fA-F]{6,})`.
- Replace each whole protected element (the tag carrying `data-cfemail` through its closing tag) with the decoded email as plain text, and each `email-protection#hex` href with `mailto:{decoded}`, so the email gets real surrounding context. method = `cloudflare`.
- Wrap decode in try/except; skip on failure.

**5b. Normalize HTML** (`normalize_html.py`), applied in this order to the Cloudflare-decoded HTML:
1. `@` -> `@`, `.` -> `.` (case-insensitive, literal backslash sequences); other `\u00XX` -> space.
2. `&#64;`, `&#064;`, `&#x40;`, `&commat;` -> `@`; `&#46;`, `&#x2e;`, `&period;` -> `.`
3. `%40` -> `@`, `%20` -> space.
4. Remove zero-width chars `​ ‌ ‍ ﻿`.
5. `\s*[\[\(\{]\s*at\s*[\]\)\}]\s*` -> `@` and `\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*` -> `.` (case-insensitive).
6. `\b([a-z0-9._%+-]+)\s+at\s+([a-z0-9-]+)\s+dot\s+(com|net|org|us|co|biz|info|io)\b` -> `\1@\2.\3` (case-insensitive). This must not rewrite ordinary sentences like "we at Acme build roofs".

**5c. Sources** (`emails.py`), all on the normalized HTML:

| Method | Rule |
| --- | --- |
| `mailto` | every `href` starting `mailto:` (case-insensitive): take text after `mailto:`, cut at `?`, URL-decode, split on `,` for multiple recipients |
| `jsonld` | parse each `<script type="application/ld+json">` with `json.loads` (skip on error); walk recursively including `@graph` lists; collect any `email` value (strip `mailto:`) |
| `text` | regex over visible text (HTML with tags replaced by spaces, entities unescaped) |
| `attribute` | regex over raw attribute values (`data-email`, `value`, `content`, any attribute) |
| `script` | regex over JSON-LD script bodies not already parsed, and any inline JSON left after fetch |

Email regex (case-insensitive):
```
[a-z0-9][a-z0-9._%+-]{0,63}@(?:[a-z0-9-]+\.)+[a-z]{2,24}
```
Post-process each match: lowercase, strip, remove trailing dots. The alphabetic TLD requirement is deliberate (rejects `lib@4.2.1`).

When the same email is found by several methods, keep one candidate with the highest-precision method in this order: mailto, jsonld, cloudflare, text, attribute, script, and keep all source URLs.

**5d. Context** (`context.py`)
- Build visible text of the page: remove non-JSON-LD scripts and styles, replace `mailto:` anchors with ` {email} `, replace tags with spaces, unescape entities, collapse whitespace.
- Context = 120 characters before and after the first occurrence of the email in that text, trimmed. If not found, empty string.
- Also record the page `<title>` (first 150 chars).

**5e. Phones** (`phones.py`)
- Sources: `tel:` hrefs and `phonenumbers.PhoneNumberMatcher(visible_text, "US")`.
- Keep only `is_valid_number`. Format E.164. Dedupe. Order: tel: links first, then by first appearance. Max 5.

**5f. Socials** (`socials.py`)
- From all hrefs, keep the first URL per network: facebook.com, instagram.com, linkedin.com (`/company/` or `/in/`), x.com or twitter.com, youtube.com, tiktok.com, yelp.com.
- Drop share/intent links: paths containing `sharer`, `share`, `intent`, `dialog`, `plugins`.

**5g. Contact form** (`forms.py`)
- A page has a contact form if it contains a `<form>` with an `input[type=email]`, a `textarea`, or an input whose `name`/`id` contains `email` or `message`; or it embeds a known form provider: `hsforms`, `hbspt.forms`, `jotform`, `typeform`, `gravityforms`, `wpforms`, `formstack`, `cognitoforms`, `wufoo`, `formsite`.
- `contact_form_url` = highest-scored page with a form (homepage counts last).

### Stage 6: Filter and rank (`filter_rank.py`)

**Drop a candidate if any rule matches:**
- Ends with an asset extension: `.png .jpg .jpeg .gif .webp .svg .bmp .ico .css .js .json .woff .woff2 .ttf .eot .mp4 .webm .avif .tif .tiff`
- Domain (or a parent domain) in: `example.com example.org example.net domain.com yourdomain.com yoursite.com website.com email.com mysite.com test.com sentry.io wixpress.com sentry-next.wixpress.com`
- Local part exactly one of: `noreply no-reply donotreply do-not-reply mailer-daemon postmaster name yourname your.name you user username email youremail your-email firstname first.last firstname.lastname johndoe john.doe janedoe jane.doe johnsmith test example`
- Local part matches `^[a-f0-9]{12,}$` or `^\d+$`
- Local part longer than 64 or total longer than 254

**Classify each survivor:**
- `own`: email domain == site domain, or one ends with `.` + the other (use `final_domain`).
- `role`: local part exactly one of `info hello contact sales support office admin team service help inquiries enquiries booking bookings appointments estimates quotes marketing hr careers jobs billing accounts privacy legal media press webmaster`
- `free`: email domain's first label in `gmail yahoo hotmail outlook aol icloud msn live comcast att sbcglobal verizon bellsouth me ymail protonmail mail`

**Rule rank (1 = best):**
1. own and not role
2. own and role
3. free
4. everything else (third party or unknown)

Ties: mailto/jsonld method before others, then found on a higher-scored page, then first appearance.

`rules_best` = top-ranked candidate. Cap candidates at 50 (keep the best-ranked 50).

### Stage 7: TypeSafe judgment (`typesafe.py`)

**Call TypeSafe only if:** 2 or more candidates remain, OR any candidate is rank 3 or 4. Skip if 0 candidates or exactly 1 rank-1/rank-2 candidate.

**Request**
```
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer {TYPESAFE_API_KEY}
Content-Type: application/json
```
Body (build exactly this shape; `N` = candidate count, index order = rule rank order):
```json
{
  "model": "jev-latest",
  "state": {
    "business_domain": "<final_domain>",
    "page_title": "<homepage title>",
    "candidates": [
      {"email": "<email>", "context": "<context>", "source_url": "<first source url>"}
    ]
  },
  "questions": {
    "best_contact": {
      "type": "choice",
      "instructions": "Which email is the best way to reach the owner or a decision maker at the business that owns <final_domain>? Prefer a named person at the business over a generic inbox. Emails for the web designer, hosting platform, software vendor, or other third parties do not count.",
      "criteria": {
        "<email 0>": null,
        "<email 1>": null,
        "none": "None of these emails belongs to this business."
      }
    },
    "site_status": {
      "type": "choice",
      "instructions": "What kind of website is <final_domain>?",
      "criteria": {
        "operating_business": "A real business that is currently operating and selling products or services.",
        "parked_or_for_sale": "A parked domain, placeholder, or domain listed for sale.",
        "other": "Something else, such as a personal blog, directory, or closed business."
      }
    },
    "belongs_0": {
      "type": "noul",
      "instructions": "Does the email <email 0> belong to the business that owns <final_domain> (the owner, an employee, or a company inbox), rather than a web designer, platform, vendor, or other third party?"
    }
  }
}
```
One `belongs_i` per candidate. Timeout 30s. Concurrency `TYPESAFE_CONCURRENCY`. On 429 or 529: exponential backoff 2s, 4s, 8s, 16s, 32s then give up. On any failure: fall back to rules (below), set `typesafe_error=true`.

**Decision logic**
```
pick = answers.best_contact
belongs[email] = answers["belongs_i"].noul
accepted = pick.choice != "none"
           and pick.confidence >= TYPESAFE_CONFIDENCE_MIN
           and belongs[pick.choice] >= TYPESAFE_BELONGS_MIN

if accepted:
    best_email = pick.choice; best_email_source = "typesafe"; needs_review = false
elif pick.choice == "none" and pick.confidence >= TYPESAFE_CONFIDENCE_MIN:
    best_email = null; best_email_source = "typesafe"; needs_review = false
    status = "no_business_email"
else:
    best_email = rules_best; best_email_source = "rules"; needs_review = true

business_emails = [e for e in candidates if belongs[e] >= TYPESAFE_BELONGS_MIN]
site_status = answers.site_status.choice
```
Safety check: assert `best_email` is in the candidate list; if not, discard and use `rules_best` (should never happen).

When TypeSafe is skipped: `best_email = rules_best`, `best_email_source = "rules"`, `business_emails` = all rank 1 and 2 candidates, `confidence = null`, `needs_review = false`, `site_status = null` unless parked detection fires.

### Parked detection (`parked.py`, deterministic, runs after homepage fetch)

Mark `parked_or_for_sale` if any:
- NS host contains: `sedoparking`, `bodis`, `parkingcrew`, `above.com`, `dan.com`, `afternic`, `hugedomains`, `namebrightdns`, `parklogic`
- Final URL host is on: `dan.com`, `afternic.com`, `hugedomains.com`, `sedo.com`, `godaddy.com/forsale`, `undeveloped.com`
- Homepage visible text (lowercase) contains any: "this domain is for sale", "buy this domain", "domain may be for sale", "this domain has been registered", "parked free", "future home of", "coming soon" AND visible text is under 300 characters.

Parked domains skip stages 3 to 7.

---

## 7. Status assignment (`status.py`)

Exactly one per domain, first match wins:

| Status | Rule |
| --- | --- |
| invalid_input | Input could not be parsed into a domain |
| fetch_failed | DNS failed or homepage fetch failed |
| parked_or_for_sale | Parked detection fired, or TypeSafe site_status = parked_or_for_sale with confidence >= 0.8 |
| no_business_email | TypeSafe chose "none" with confidence >= min |
| found | best_email not null |
| form_only | no email, contact_form_url not null |
| no_contact_info | no email and no form (phones may still be present) |

---

## 8. Output record

`schemas.py: DomainResult`

| Field | Type |
| --- | --- |
| domain | str |
| final_domain | str or null |
| status | str (section 7) |
| best_email | str or null |
| best_email_source | "typesafe" or "rules" or null |
| confidence | float or null |
| needs_review | bool |
| business_emails | list[str] |
| all_emails | list[{email, source_urls, method, rank, belongs}] |
| phones | list[str] (E.164) |
| socials | dict[str, str] |
| contact_form_url | str or null |
| mx_provider | "google", "microsoft", "other", "none", or null |
| site_status | str or null |
| pages_fetched | list[str] |
| jina_tokens | int |
| typesafe_called | bool |
| typesafe_error | bool |
| error_reason | str or null |
| processed_at | datetime |

**CSV columns** (results.csv), in order:
`row_index, input_value, domain, final_domain, status, best_email, best_email_source, confidence, needs_review, business_emails, all_emails, phones, contact_form_url, facebook, instagram, linkedin, x, youtube, tiktok, yelp, mx_provider, site_status, pages_fetched, error_reason`

List fields are joined with `, `. `all_emails` in CSV is emails only (no metadata).

---

## 9. Scale and job behavior (50k rows)

- **Dedupe**: one task per unique root domain per job. All job_items with that domain read the same result.
- **Cache**: before enqueueing, if `domains.finished_at` is within `CACHE_DAYS` and status is not `fetch_failed`, reuse the result without refetching. `POST /jobs` accepts `"fresh": true` to bypass.
- **Workers**: arq with two queues: `fetch` (stages 1 to 6, `max_jobs=GLOBAL_FETCH_CONCURRENCY`) and `judge` (stage 7, `max_jobs=TYPESAFE_CONCURRENCY`). A domain that needs TypeSafe is enqueued to `judge` with its candidates persisted in `domains.result` (partial), so TypeSafe backoff never blocks fetching.
- **Checkpoint**: `domains.stage` updated after each stage; finished results written once, atomically. A restarted job only enqueues domains whose `stage != done`.
- **Job completion**: job is `done` when every unique domain has a status. Update `jobs.done_count` in batches every 5 seconds, not per domain.
- **Expected volume per 50k domains** (verify with eval): ~100k robots/sitemap fetches, ~200k page fetches, 15k to 25k TypeSafe calls, ~40 GB HTML transferred.

---

## 10. Logging and metrics

- Structured JSON logs. One `domain_done` line per domain: domain, status, pages_fetched count, candidates count, typesafe_called, jina_tokens, duration_ms.
- Never log page HTML or API keys.
- Per job, keep running totals in Redis: done, failed, found, typesafe_calls, jina_tokens. `GET /jobs/{id}` reads these.

---

## 11. Tests

**Unit tests** with fixtures in `tests/fixtures/`. Each must pass before moving on:

| Fixture | Expected |
| --- | --- |
| mailto_subject.html: `<a href="mailto:Mike@AcmeRoofing.com?subject=Hi">` | `mike@acmeroofing.com`, method mailto |
| cf_span.html: `data-cfemail` span for info@acmeroofing.com | decoded, method cloudflare, context includes surrounding words |
| cf_link.html: `/cdn-cgi/l/email-protection#...` link | decoded |
| obfuscated.html: `acmeroof [at] gmail [dot] com`, `jane at acmeroofing dot com` | both found |
| sentence.html: "We at Acme build roofs." | no email |
| entities.html: `billing&#64;acmeroofing.com`, `@` variant | found |
| jsonld_graph.html: email nested in `@graph` Organization | found, method jsonld |
| junk.html: `logo@2x.png`, `7f3a9c2b1d4e5f60a1b2@sentry.io`, `you@example.com`, `noreply@acmeroofing.com`, `core-js@3.2.1` | none returned |
| designer_footer.html: owner email + `hello@bluepixel.com` in "Website by" footer | rank order own > third party |
| parked.html + parked NS | status parked_or_for_sale, no page fetches |
| robots.txt with 2 Sitemap lines | both parsed |
| sitemap_index.xml -> page-sitemap.xml.gz | page URLs collected, product-sitemap skipped |
| links scoring set | contact > quote > about > privacy; blog/2024 URLs excluded |

**Integration test**: mock Jina and TypeSafe with respx; run `process_domain` end to end on a fixture site with homepage + contact page; assert full DomainResult.

**Eval harness** (`scripts/eval.py`):
- Input CSV: `domain, correct_emails` (semicolon-separated; empty means site has no email) for 50 hand-labeled domains, plus 150 unlabeled domains.
- Runs the pipeline on all 200 (fresh), writes results, prints:
  - hit rate (all 200): domains with best_email not null
  - precision (50 labeled): best_email in correct_emails
  - third-party leak rate (50 labeled): best_email not in correct_emails and not own-domain
  - TypeSafe call rate, needs_review rate
  - near-empty HTML rate: homepage visible text under 200 chars (tracks JS-only sites for later)
  - avg jina_tokens per domain, avg duration
- Run twice: `--no-typesafe` (rules only) and default. Print both side by side.

---

## 12. Acceptance criteria

The build is done when, on the 200-domain eval set:
- Hit rate >= 70%
- Precision >= 95% on the 50 labeled domains
- Third-party leak rate < 2%
- needs_review rate < 10%
- No output email ever absent from the fetched pages (enforced by assertion)
- A 5,000-domain job completes without manual intervention, survives a worker restart mid-run, and produces results.csv with one row per input row

---

## 13. Build order

1. Scaffold repo, Docker Compose, settings, DB models, migrations. `POST /extract` returning a stub.
2. Fetch layer (JinaFetcher + HttpxFetcher) with rate limiting and retries. Verify Jina returns raw HTML and keeps JSON-LD (Stage 4 milestone check).
3. Stage 1 and 2: normalize, DNS, homepage, parked detection.
4. Stage 3: robots, sitemaps, links, scoring. Unit tests.
5. Stage 5 and 6: extraction and filter/rank. All fixture tests passing.
6. Eval harness, run rules-only baseline on the 200 domains. Report numbers.
7. Stage 7: TypeSafe. Rerun eval, compare against baseline. If TypeSafe changes under 5% of best emails, report it before tuning.
8. Jobs API, arq queues, checkpointing, cache, CSV/JSON results, webhook.
9. 5,000-domain soak test. Then 50k.

Stop after each step and report what was built and test results before starting the next.
