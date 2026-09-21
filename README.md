# Website Email Extractor

Takes business website URLs and returns the contact emails published on those sites,
ranked, with one status per domain. Full contract: [SPEC.md](SPEC.md).

Code finds every email; the model only *chooses* among emails found verbatim on a fetched
page. No email is ever emitted that did not appear in fetched HTML.

## Run

```bash
cp .env.example .env      # then fill JINA_API_KEY, TYPESAFE_API_KEY, API_KEY
docker compose up --build -d
docker compose exec api python -m app.users add "Sam"     # prints Sam's key once
```

`migrate` runs `alembic upgrade head` first; `api` then serves on :8000 and `worker`
(`python -m app.worker`) processes jobs. The port binds to `127.0.0.1` unless
`BIND_ADDR` is set in `.env` -- set it to the machine's Tailscale IP to share it with
the team over the tailnet.

## Users

Each person gets their own key: `python -m app.users add NAME [--admin]`, `list`,
`revoke NAME`. Only a sha256 of the key is stored. A person sees only their own jobs
(someone else's job is a 404) and may have `MAX_ACTIVE_JOBS_PER_USER` unfinished jobs
at once. `API_KEY` from `.env` is the bootstrap admin key and sees every job.

## API

Every route except `/healthz` and `/` requires `X-API-Key` (401 otherwise).

| Route | What it does |
| --- | --- |
| `GET /` | upload page (spec §14) |
| `POST /jobs` | JSON `{"items": [...]}` or a multipart file upload; queues the job |
| `POST /jobs/preview` | detects the website column; creates nothing |
| `GET /jobs` | the caller's 50 most recent jobs |
| `GET /jobs/{id}` | progress: done, running, retrying, found, failed, cached, jina_tokens |
| `GET /jobs/{id}/results.csv` / `.json` | the uploaded columns plus `ex_*` result columns, in row order; unfinished rows are `pending` |
| `POST /jobs/{id}/resume` | resume a paused job (e.g. after topping up Jina) |
| `POST /jobs/{id}/cancel` | stop claiming the job's domains |
| `POST /extract` | one domain, synchronously |

```bash
curl -s localhost:8000/jobs -H "X-API-Key: $KEY" -F file=@leads.csv
```

## How jobs run

The queue is Postgres (`app/queue.py`), not arq. arq is a single FIFO list: a 50k-row
job queued first would hold every later job until it drained, and one arq job id per
domain silently dropped the same domain from a second job.

- **Fair sharing.** Each claim goes to the user with the fewest domains in flight,
  then that user's job with the fewest, then the oldest. Verified live: a 6-row job
  submitted after a 39-row job took about half the slots and finished in 25s.
- **Shared domains are fetched once.** A per-claim lock on `domains` makes a second job
  wait for the first and then read its result; only the job that fetched pays tokens.
- **Cache.** Results within `CACHE_DAYS` are reused unless the job has `fresh=true`;
  `fetch_failed` results are never reused from the cache.
- **Crash safety.** Workers heartbeat every 20s; rows silent for 90s are reaped back to
  the queue. SIGTERM hands in-flight rows back without spending an attempt.
- **Jina account errors** (402 etc.) pause every active job with
  `pause_reason=jina_account` and mark no domain failed. Resume after topping up.
- **Rate limit.** `RATE_LIMIT_BACKEND=redis` puts the token bucket in Redis, so the API
  and every worker share one `JINA_RPM`. If Redis is down it falls back to in-process.
- **Webhook.** Each finished domain is POSTed as `{job_id, row_indexes, result}`,
  retried after 2s, 8s and 30s.

## Local dev (no Docker)

```bash
uv venv --python 3.12 && uv pip install -r requirements-dev.txt
uv run pytest
```

Point `DATABASE_URL`/`REDIS_URL` at `localhost` instead of the compose hostnames.
The queue tests need a Postgres they may wipe:
`TEST_DATABASE_URL=postgresql+asyncpg://.../extractor_test uv run pytest tests/test_queue_pg.py`.

## Secrets

`.env` is gitignored and holds the real keys. `.env.example` holds placeholders only.
Key values are never logged.

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
- [ ] 9. 5k soak test through the running service, then 50k
