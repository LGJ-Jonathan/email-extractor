# Website Email Extractor

Takes business website URLs and returns the contact emails published on those sites,
ranked, with one status per domain. Full contract: [SPEC.md](SPEC.md).

Code finds every email; the model only *chooses* among emails found verbatim on a fetched
page. No email is ever emitted that did not appear in fetched HTML.

The team uses it through the **Email Extractor** page of the HTM portal
(clients.leadgenjay.com/email-extractor): upload a CSV/XLSX or paste domains, confirm the
website column, watch progress, download results. The portal talks to this service
server-side; nobody needs a key of their own.

## Where it runs

Production is the Railway project **high-ticket-portal**, next to the portal, in the
same region:

| Service | What | Notes |
| --- | --- | --- |
| `email-extractor` | API (`ROLE=api`, `python -m app.serve`, port 8080) | No public domain. The portal reaches it at `http://email-extractor.railway.internal:8080` over Railway's private network. 2 GB. |
| `email-extractor-worker` | Worker (`ROLE=worker`, `python -m app.worker`) | One process, 50 domains at a time. 2 GB, 30 s drain on redeploy. |
| `Postgres` | Jobs, queue, results cache, users, keys | Private only. |
| `Redis` | Shared Jina rate limit, per-person request limit | Private only; losing it is harmless. |

Both extractor services build this repo's **`master`** branch from the `Dockerfile`;
`scripts/start.sh` picks the process from `ROLE`. `alembic upgrade head` runs as the
pre-deploy command on both (an advisory lock makes the concurrent runs safe).
A push to `master` redeploys both.

Variables (set per service in Railway; values never in git):

| Variable | Service | Purpose |
| --- | --- | --- |
| `DATABASE_URL`, `REDIS_URL` | both | references to `${{Postgres.DATABASE_URL}}` / `${{Redis.REDIS_URL}}` |
| `JINA_API_KEY`, `TYPESAFE_API_KEY` | both | fallback keys; a key saved from the portal overrides them |
| `PROVIDER_KEY_ENCRYPTION_KEY` | both, **identical** | encrypts keys saved from the portal (base64 of 32 bytes) |
| `API_KEY` | api | bootstrap admin key, ≥ 32 characters |
| `ROLE`, `PORT` | per service | `api` + `8080` / `worker` |
| `RATE_LIMIT_BACKEND=redis`, `JINA_RPM`, `GLOBAL_FETCH_CONCURRENCY`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | both | throughput and connections (API 10+10, worker 20+20, under Postgres's 100) |

The portal needs `EMAIL_EXTRACTOR_BASE_URL` and `EMAIL_EXTRACTOR_API_KEY` (a service key,
below) on its `high-ticket-portal` service.

## Who can do what

- **Service key (the portal).** `python -m app.users add portal --service`. It must name
  the signed-in person in `X-Acting-User` (their verified email); everything --
  ownership, fair sharing, limits, visibility -- then applies to that person.
  `X-Acting-Role: admin` (portal admins) sees everyone's jobs. Both headers are refused
  from any other key.
- **Personal keys.** `python -m app.users add NAME [--admin]`, `list`, `revoke NAME`.
  Only a sha256 is stored; the key is shown once.
- **Bootstrap admin.** `API_KEY`; refused if it is a placeholder or shorter than 32.

Run these inside the API service: `railway ssh --service email-extractor -- python -m app.users list`.

Limits per person: 3 unfinished jobs, 50,000 rows / 50 MB per job, 120 requests a
minute (status polling exempt). Someone else's job is a 404.

## Provider keys (Jina, TypeSafe)

Admins set them in the portal (Email Extractor → Provider keys). Each key is checked
with the provider first, stored AES-GCM encrypted under `PROVIDER_KEY_ENCRYPTION_KEY`,
and never returned (last four only). The API and worker pick it up within ~30 s, no
redeploy. Saving a Jina key with at least `JINA_RESUME_MIN_TOKENS` (10M) resumes the
jobs paused for Jina credit.

- A saved key a process cannot decrypt means **no key**, never the environment's, and
  the portal shows it as unreadable (usually: `PROVIDER_KEY_ENCRYPTION_KEY` differs
  between the two services).
- Every save and removal is audited (`provider_key_audit`); changes are limited to 10 a
  minute per caller, admins included.
- Jina balance comes from its dashboard's wallet endpoint (not in Jina's public docs).
  TypeSafe publishes no balance API, so only whether its key works is shown.

## API

Every route except `/healthz` and `/` requires `X-API-Key`.

| Route | What it does |
| --- | --- |
| `POST /jobs` | JSON `{"items": [...]}` or multipart file (+ `column`, `fresh`, `webhook_url`); optional `Idempotency-Key` |
| `POST /jobs/preview` | detects the website column (or previews `column=`); creates nothing |
| `GET /jobs` | the caller's 50 most recent jobs, with progress (admins: everyone's, with owner) |
| `GET /jobs/{id}` | status, counts by outcome, needs review, captured, tokens, `active_seconds`, columns |
| `GET /jobs/{id}/recent` | most recently finished domains |
| `GET /jobs/{id}/results.csv` / `.json` | uploaded columns + `ex_*` result columns, row order; unfinished rows `pending` |
| `POST /jobs/{id}/resume` / `cancel` | resume a paused job / stop one |
| `GET /estimate?domains=N` | time range for N domains at this person's fair share of recent speed |
| `GET /providers/status` | Jina balance, TypeSafe key status, this month's usage |
| `GET` / `PUT` / `DELETE /providers/keys[/{jina\|typesafe}]` | admin: view / check-and-save / remove provider keys |
| `POST /extract` | one domain, synchronously |

Every error is `{"error": {"code", "message", "retry_after", "details"}, "detail",
"request_id"}` with `Retry-After` / `X-Request-ID` headers; the codes are listed in
`app/errors.py` (`ERROR_CODES`). A site with no email is not an error: it is a 200
result with `status` and `error_reason`.

## How jobs run

The queue is Postgres (`app/queue.py`), not arq: arq is one FIFO list, so a 50k-row job
would hold every later job, and one arq job id per domain dropped a domain from a
second job.

- **Fair sharing.** Each free slot goes to the person with the fewest domains in flight,
  then their least-served job, then the oldest. A 200-row upload starts at once beside
  a 50k one. Total speed is shared: ~40-45 domains/min at `JINA_RPM=500`.
- **Shared domains are fetched once**; a per-claim lock makes the second job read the
  first one's result. Results within `CACHE_DAYS` (90) are reused unless `fresh=true`.
- **Each job keeps what it received** (a snapshot in `job_domains`), so a finished job's
  download never changes.
- **Crash safety.** Heartbeats every 20 s, rows silent for 90 s are reaped; SIGTERM
  hands in-flight rows back without spending an attempt.
- **Jina out of credit** (402) pauses every active job and fails no domain.
- **Uploads.** At most `MAX_CONCURRENT_UPLOADS` (2) files are parsed at once (a 50 MB
  file peaks at ~400 MB); others wait up to 40 s, then get 503 `busy`.
- **Webhook** (optional): each finished domain and a final `job.done`, HMAC-signed with
  `WEBHOOK_SECRET`, to public https URLs only, through a bounded sender.

## Local dev

```bash
uv venv --python 3.12 && uv pip install -r requirements-dev.txt
uv run pytest
```

The queue tests need a Postgres they may wipe:
`TEST_DATABASE_URL=postgresql+asyncpg://.../extractor_test uv run pytest`.
`docker compose up --build` still runs the whole stack locally (the port binds to
`127.0.0.1` unless `BIND_ADDR` is set).

## Secrets

`.env` is gitignored; `.env.example` holds placeholders only. Keys are never logged:
httpx/httpcore are held at WARNING (httpx logs full request URLs, and Jina's wallet
endpoint takes the key in the query string), and a filter masks anything key-like in
every log line (`app/logsetup.py`).

## Verified deviations from SPEC.md

Each was measured against the live Jina API on 2026-09-19 and each is authorised by the
Stage 4 milestone check, which tells us to verify these exact things and correct them.

| Spec says | Reality | What the code does |
| --- | --- | --- |
| page is in `data.content` | no such field; `data.html` (html mode), `data.text` (text mode) | reads `data.html` |
| `script:not([type="application/ld+json"])` keeps JSON-LD | strips **all** scripts, JSON-LD included | selector dropped; non-JSON-LD scripts stripped in code, both backends |
| `get_text` uses Jina text mode | collapses newlines, so `(?im)^\s*sitemap:` matches nothing and sitemap discovery finds zero sitemaps on every domain | `get_text` always uses httpx, whatever `FETCH_BACKEND` is |
| §17 error table omits 422 | Jina returns 422 `AssertionFailureError` for non-HTML content types (verified: `sqlite.org` serves `application/octet-stream`) | classified Permanent / `not_html`, not Bug — otherwise it feeds the `internal_error` alarm that pauses a job at 2% |

Two judgement calls made in step 3, both reversible:

- **Site-builder hosts keep their full host as the crawl key.** `tldextract` maps
  `bobshvac.squarespace.com` to `squarespace.com`, which would collapse every
  Squarespace-hosted business in a list into one crawl sharing one cached result.
  `include_psl_private_domains=True` fixes wixsite/myshopify/blogspot but *not*
  squarespace.com, godaddysites.com or weebly.com — proven in
  `test_psl_private_domains_alone_would_not_have_fixed_this`. An explicit
  `SITE_BUILDER_SUFFIXES` list sits on top of it.
- **Parked text rules split in two.** The spec's "contains any of [7 phrases] AND text
  under 300 chars" has undefined AND scope. Explicit for-sale phrases are now conclusive
  at any length; weak phrases ("coming soon") keep the length guard. Either single
  reading is worse: one misses wordy parking pages, the other kills one-page businesses.

Step 4 corrected six discovery defects, each with a test naming the case:

| Defect | Effect | Fix |
| --- | --- | --- |
| Child-sitemap filter was a substring match over the whole URL, host included | `tag` killed heritageroofing/cottagekitchens/advantagehvac/stagecoachmoving; `author` killed authorityplumbing; `news` killed newsomeelectric — all sitemap discovery lost | whole-token match on the last path segment |
| Exclusions written as `/blog/` but trailing slashes normalised away first | every blog/news/category/product **index** page competed for the four slots | anchored segment match on the raw path |
| `/20\d\d/` year rule | missed `/2024-annual-report` | whole-segment `^(19\|20)\d\d$` |
| Extension **allowlist** | excluded `/locations/st.-louis`, `/about/dr.-smith`, and every `.asp`/`.cfm`/`.jsp`/`.shtml` site | asset **blocklist** |
| No canonicalisation before dedupe; sort not total; one page per tier | five spellings of `/contact` ate four slots; `/contact-us` unreachable while `/privacy` was forced in | canonical key, `url` as final tiebreak, per-tier cap 2 |
| Stop at the first valid sitemap | a product sitemap listed first hid the contact page *and* suppressed the fallback | union all robots sitemaps up to the cap |

Two further fixes came from running it live rather than from the audit: anchor keywords
now match whole words (`roundabout` no longer matched `about`), and an anchor-only match
(score 10, no tier) no longer counts toward the fallback threshold — on nginx.org two
such links were suppressing the fixed-path fallback, so the crawl took a docs page
instead of trying `/contact`.

Step 5 corrected nine extraction and ranking defects. The two that violated Core Rule 1
(an address that never appeared on the page):

| Defect | Fabrication it produced |
| --- | --- |
| Rule 6 rewrote `<word> at <word> dot <tld>` anywhere | "Order online at acmeroofing dot com" became `online@acmeroofing.com`, which is own-domain and non-role, so it ranked **1** and beat the site's real `info@`. Now requires a contact cue within 40 chars; the bracketed forms of rule 5 are untouched |
| The email regex truncated instead of rejecting | a 66-char local part matched its last 64; `_private@` yielded `private@`; `-mike@` yielded `mike@`. A lookbehind now refuses a match starting mid-token |

The rest: `method="cloudflare"` was unreachable by construction (the §11 fixture could
never pass); odd-length `data-cfemail` hex silently appended a junk character; rule 1's
"other `\u00XX` -> space" destroyed the `\u0040` it was meant to convert; JSON-LD bodies
are now excluded from normalisation so `json.loads` still sees valid JSON; the Stage 6
deny list became a pattern (`no_reply@`, `wordpress@`, `bounce@`, `root@` all outranked
`info@` because the list held only exact spellings); the `own` test's reverse clause made
`bob@co.uk` own-domain for `acme.co.uk`; `free` matched the first host label so
`info@mail.bluepixel.com` scored as a free provider; `x.com` substring-matched netflix,
dropbox, box, **wix** and linux; the contact-form detector fired on Mailchimp footers,
comment boxes and login forms; and `PhoneNumberMatcher` turned licence, order and DUNS
numbers into phone numbers.

One defect was found by the fixtures rather than the audit, in code written this step:
`fix_tld` trimmed an invalid TLD back to *any* valid suffix, so `sha256@abc.def` became
`sha256@abc.de`, `logo@2x.png` became `logo@2x.pn` and `hero@2x.jpg` became `hero@2x.jp`
— three new fabrications. Trimming is now restricted to common TLD prefixes.

Known and unaddressed: the token bucket, semaphores and circuit breaker are per worker
process. With N worker processes the effective rate is N x `JINA_RPM`. Redis-backing them
is a step-8 concern; the interfaces in `app/ratelimit.py` are narrow so the swap is
mechanical.

## Fetch stability

The first 16,261-domain run finished at 44.0% found, and a hand-built retry pass over
its transient failures lifted that to 55.4%. Plotting `circuit_open` against found-rate
in completion order showed why: the breaker oscillated for the whole seven hours.
Stretches with it quiet found 56-62% of addresses; stretches with it thrashing found
24-33%. All 3,095 `circuit_open` rows had `n_pages=0` -- no page was ever fetched for
them, so nothing was wasted except the result.

| Defect | Effect | Fix |
| --- | --- | --- |
| `_exc_to_fetch_error` blamed every transport failure on the provider | r.jina.ai is a proxy, so a slow business site makes our request to Jina slow. At `READ_TIMEOUT_S=7` any site over 7s voted "Jina is unhealthy" | only `ConnectError`/`ConnectTimeout`/`PoolTimeout` count against provider health; a `ReadTimeout` after a successful connect is the target |
| `X-Timeout` was `connect + read` = 10s against a client read timeout of 7s | we always abandoned first, so Jina's own classified answer for a slow page was never read -- and, before the fix above, became a provider failure | `X-Timeout` is `JINA_TIMEOUT_S`; the Jina call gets a per-request client budget 5s wider. Direct target fetches keep the tight one |
| `probes_needed=4` of 5 | an 80% bar to recover, while the breaker opens at a 30% failure rate -- half-open usually failed and reopened at once. That is the oscillation | `probes_needed=3`, plus open windows that double to `max_open_seconds` and reset only after holding closed |
| `CircuitOpen` ended the domain | provider backpressure was recorded as the domain's own `fetch_failed`, and `max_domain_attempts` / `retry_pass_delay_s` were dead settings | `process_domain` retries the whole domain up to `MAX_DOMAIN_ATTEMPTS`, sleeping out the breaker's remaining window. Each attempt gets its own deadline |

Still open: the local `TokenBucket.acquire` has no fairness or jitter, so 50 waiters
wake together and the tail feeds `domain_timeout` (the Redis bucket jitters); the
breaker and semaphores are per process, which is fine for the one-worker deployment
but means a second worker process gets its own breaker; and nothing persists
`last_open_reasons`, so a storm can only be reconstructed afterwards from logs.
`release_domain` is now called by the worker once a domain finishes.

## Build progress

Build order is SPEC.md section 13; each step stops and reports before the next starts.
Section 15 edge cases are implemented inside the step that owns their stage.
`SPEC.v1.md` is the superseded first revision, kept only for diffing.

- [x] 1. Scaffold: compose, settings, DB models, migrations, `POST /extract` stub
- [x] 2. Fetch layer (Jina + httpx), rate limiting, retries, §17 level-1 + breakers, §16 connections
- [x] 3. Stages 1-2: normalize, DNS, homepage, parked detection, §15 input cases
- [x] 4. Stage 3: robots, sitemaps, links, scoring, §16 wave 1 + selection shortcut
- [x] 5. Stages 5-6: extraction, filter/rank, §11 fixtures, §16 wave 2/3 early stop
- [x] 6. Eval harness, rules-only baseline
- [x] 7. Stage 7: TypeSafe
- [x] 8. Jobs API, Postgres queue (replaces arq), per-user keys, cache, CSV/JSON, webhook, upload page (§14)
- [x] Portal integration: service key + acting user, error contract, estimates, provider keys; on Railway
- [ ] 9. 5k soak test through the running service, then 50k (needs Jina credit)
