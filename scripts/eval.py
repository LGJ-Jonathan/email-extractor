#!/usr/bin/env python
"""Eval harness (spec section 11).

Input CSV needs a website column; an email column, when present, supplies the labels
(`correct_emails`, semicolon-separated; empty means the site has no findable email).

    uv run python scripts/eval.py FILE.csv --limit 100 --concurrency 8
    uv run python scripts/eval.py FILE.csv --no-typesafe      # rules-only baseline
"""

import argparse
import asyncio
import contextlib
import csv
import logging
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.pipeline.fetch import build_fetcher, build_gate, close_client  # noqa: E402
from app.pipeline.normalize import normalize_input  # noqa: E402
from app.pipeline.run import process_domain  # noqa: E402
from app.schemas import DomainResult  # noqa: E402

WEBSITE_COLS = ("website", "websiteurl", "companywebsite", "companydomain", "domain", "url", "site")
EMAIL_COLS = ("email", "contactemail", "workemail", "emailaddress")

STATS: dict[str, list] = {"homepage_text_len": [], "duration_ms": []}


class DomainDoneCapture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() != "domain_done":
            return
        for key in STATS:
            if hasattr(record, key):
                STATS[key].append(getattr(record, key))


def norm_header(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def pick_column(headers: list[str], candidates: tuple[str, ...]) -> str | None:
    normed = {norm_header(h): h for h in headers}
    for want in candidates:
        if want in normed:
            return normed[want]
    return None


def load_rows(path: pathlib.Path, website_col: str | None, email_col: str | None):
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        wcol = website_col or pick_column(headers, WEBSITE_COLS)
        ecol = email_col or pick_column(headers, EMAIL_COLS)
        if not wcol:
            raise SystemExit(f"no website column found in {headers}")
        rows = []
        for row in reader:
            rows.append((row.get(wcol, ""), (row.get(ecol) or "") if ecol else ""))
    return rows, wcol, ecol


def own_domain(email: str, site: str) -> bool:
    from app.pipeline.filter_rank import is_own, local_and_domain

    return is_own(local_and_domain(email)[1], site)


ROW_HEADER = ["input", "label", "domain", "final_domain", "status", "best_email",
              "rank1_method", "all_emails", "phones", "form", "tokens", "reason",
              "n_pages", "best_from", "best_page_index", "pages",
              "n_emails", "emails_ranked"]


def row_for(raw: str, label: str, r: DomainResult) -> list:
    # Which page the winning address came from: the number that decides whether smarter
    # page RANKING is worth a per-domain model call, or whether we should simply fetch
    # fewer pages.
    best_from = ""
    best_idx = ""
    if r.best_email:
        for c in r.all_emails:
            if c.email == r.best_email and c.source_urls:
                best_from = c.source_urls[0]
                if best_from in r.pages_fetched:
                    best_idx = str(r.pages_fetched.index(best_from))
                break
    return [raw, label, r.domain, r.final_domain or "", r.status, r.best_email or "",
            r.all_emails[0].method if r.all_emails else "",
            ", ".join(e.email for e in r.all_emails), ", ".join(r.phones),
            r.contact_form_url or "", r.jina_tokens, r.error_reason or "",
            len(r.pages_fetched), best_from, best_idx, " | ".join(r.pages_fetched),
            len(r.all_emails),
            " | ".join(f"{c.email}:r{c.rank}:{c.method}" for c in r.all_emails)]


async def run(rows, concurrency: int, out: pathlib.Path, every: int = 100):
    """Stream results to disk as they finish, so a long run survives a crash."""
    gate = build_gate()
    import app.pipeline.run as _run
    _run._shared_gate = gate          # so the report can read its breaker
    fetcher = build_fetcher(gate)
    sem = asyncio.Semaphore(concurrency)
    results: list[tuple[str, str, DomainResult | None]] = []
    out.parent.mkdir(parents=True, exist_ok=True)
    fh = out.open("w", newline="", encoding="utf-8")
    writer = csv.writer(fh)
    writer.writerow(ROW_HEADER)
    lock = asyncio.Lock()
    t0 = time.perf_counter()
    state = {"done": 0, "tokens": 0, "found": 0}

    async def record(raw: str, label: str, r: DomainResult):
        async with lock:
            results.append((raw, label, r))
            writer.writerow(row_for(raw, label, r))
            state["done"] += 1
            state["tokens"] += r.jina_tokens
            state["found"] += bool(r.best_email)
            if state["done"] % every == 0:
                fh.flush()
                el = time.perf_counter() - t0
                rate = state["done"] / el
                left = (len(rows) - state["done"]) / rate if rate else 0
                print(f"  {state['done']:>6,}/{len(rows):,}  "
                      f"found {state['found']:>6,} ({state['found']/state['done']:.1%})  "
                      f"tokens {state['tokens']:>13,}  "
                      f"{rate:5.2f}/s  eta {left/60:5.1f}m", flush=True)

    async def one(raw: str, label: str):
        norm = normalize_input(raw)
        if not norm.ok:
            await record(raw, label, DomainResult(
                domain=norm.domain or raw[:255] or "-", status="invalid_input",
                error_reason=norm.reason))
            return
        async with sem:
            await record(raw, label, await process_domain(norm.domain, fetcher))

    try:
        await asyncio.gather(*(one(w, e) for w, e in rows))
    finally:
        fh.flush(); fh.close()
        await close_client()
    return results, time.perf_counter() - t0


def report(results, wall: float, mode: str) -> dict:
    total = len(results)
    finished = [r for _, _, r in results if r]
    found = [r for r in finished if r.best_email]
    labeled = [(lab, r) for _, lab, r in results if lab.strip() and r]

    correct = leak = 0
    for lab, r in labeled:
        truth = {e.strip().lower() for e in lab.replace(",", ";").split(";") if e.strip()}
        if not r.best_email:
            continue
        if r.best_email.lower() in truth:
            correct += 1
        elif not own_domain(r.best_email, r.final_domain or r.domain):
            leak += 1

    labeled_with_pick = [r for _, r in labeled if r.best_email]
    statuses: dict[str, int] = {}
    for r in finished:
        statuses[r.status] = statuses.get(r.status, 0) + 1

    near_empty = [n for n in STATS["homepage_text_len"] if n < 200]
    tokens = [r.jina_tokens for r in finished]
    durs = STATS["duration_ms"]

    print(f"\n{'=' * 64}\n{mode}\n{'=' * 64}")
    print(f"  domains                {total}")
    print(f"  wall clock             {wall:,.1f}s")
    print(f"  hit rate               {len(found)}/{total} = {len(found)/max(total,1):.1%}")
    all_addr = sum(len(r.all_emails) for r in finished)
    with_any = sum(1 for r in finished if r.all_emails)
    print(f"  domains with >=1 email {with_any}/{total} = {with_any/max(total,1):.1%}")
    print(f"  TOTAL addresses        {all_addr:,}  ({all_addr/max(with_any,1):.2f} per domain)")
    if labeled_with_pick:
        print(f"  precision (labeled)    {correct}/{len(labeled_with_pick)} = "
              f"{correct/len(labeled_with_pick):.1%}")
        print(f"  third-party leak rate  {leak}/{len(labeled_with_pick)} = "
              f"{leak/len(labeled_with_pick):.1%}")
    print(f"  needs_review rate      {sum(1 for r in finished if r.needs_review)/max(total,1):.1%}")
    print(f"  typesafe call rate     {sum(1 for r in finished if r.typesafe_called)/max(total,1):.1%}")
    if STATS["homepage_text_len"]:
        print(f"  near-empty homepages   {len(near_empty)}/{len(STATS['homepage_text_len'])} "
              f"= {len(near_empty)/len(STATS['homepage_text_len']):.1%}  (JS-only sites)")
    if tokens:
        print(f"  jina tokens            {sum(tokens):,} total, {statistics.mean(tokens):,.0f} mean, "
              f"{statistics.median(tokens):,.0f} median")
    if durs:
        durs_s = sorted(durs)
        print(f"  domain duration        p50 {durs_s[len(durs_s)//2]/1000:.1f}s  "
              f"p95 {durs_s[int(len(durs_s)*.95)]/1000:.1f}s")
    print("  statuses               " + ", ".join(f"{k}={v}" for k, v in sorted(statuses.items())))
    try:
        from app.pipeline.run import _judge
        from app.pipeline.fetch import build_gate  # noqa: F401
        import app.pipeline.run as _run
        gate = getattr(_run, "_shared_gate", None)
        if gate is not None and gate.breaker.opened_count:
            print(f"  breaker opened         {gate.breaker.opened_count}x")
            print(f"  last trip caused by    {gate.breaker.last_open_reasons}")
        if _judge.breaker.opened_count:
            print(f"  typesafe breaker       {_judge.breaker.opened_count}x")
    except Exception:  # noqa: BLE001
        pass
    return {"hit": len(found) / max(total, 1)}


def write_csv(results, out: pathlib.Path) -> None:
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["input", "label", "domain", "final_domain", "status", "best_email",
                    "rank1_method", "all_emails", "phones", "form", "tokens", "reason"])
        for raw, label, r in results:
            if r is None:
                continue
            w.writerow([raw, label, r.domain, r.final_domain or "", r.status,
                        r.best_email or "", r.all_emails[0].method if r.all_emails else "",
                        ", ".join(e.email for e in r.all_emails), ", ".join(r.phones),
                        r.contact_form_url or "", r.jina_tokens, r.error_reason or ""])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", type=pathlib.Path)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sample", type=int, default=0, help="random sample instead of head")
    ap.add_argument("--seed", type=int, default=20260919)
    ap.add_argument("--labeled-only", action="store_true", help="keep rows with a label")
    ap.add_argument("--dedupe", action="store_true", help="one run per unique domain")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--website-col")
    ap.add_argument("--email-col")
    ap.add_argument("--no-typesafe", action="store_true", help="rules only (the step 6 baseline)")
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("scripts/eval_out/results.csv"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.CRITICAL)
    lg = logging.getLogger("email_extractor.pipeline")
    lg.setLevel(logging.INFO)
    lg.addHandler(DomainDoneCapture())

    rows, wcol, ecol = load_rows(args.csv, args.website_col, args.email_col)
    rows = [(w, e) for w, e in rows if (w or "").strip()]
    if args.labeled_only:
        rows = [(w, e) for w, e in rows if (e or "").strip()]
    if args.dedupe:
        seen: set[str] = set()
        deduped = []
        for w, e in rows:
            n = normalize_input(w)
            key = n.domain or w.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append((w, e))
        print(f"deduped {len(rows):,} -> {len(deduped):,} unique domains")
        rows = deduped
    if args.sample:
        import random
        random.Random(args.seed).shuffle(rows)
        rows = rows[: args.sample]
    elif args.limit:
        rows = rows[: args.limit]
    print(f"file    {args.csv}")
    print(f"columns website={wcol!r} email={ecol!r}")
    print(f"rows    {len(rows)}  concurrency={args.concurrency}")

    results, wall = asyncio.run(run(rows, args.concurrency, args.out))
    report(results, wall, "RULES ONLY (no TypeSafe - stage 7 is build step 7)")
    print(f"\n  wrote {args.out}")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
