"""Pipeline stages 1-3: ingest -> ticker match -> headline score, journaled.

    python -m scanner.ingest --dry-run      # fetch live, score, print; writes nothing
    python -m scanner.ingest                # same, and journal into data/scanner.db

Every source fails soft: a dead feed is logged in the report and the run continues.
Every matched item becomes a candidate row, including rejects, with the reason.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scanner.config import REPO_ROOT, KeywordConfig, Settings, load_keywords, load_settings
from scanner.journal import init_db
from scanner.matching import TickerMatch, match_item
from scanner.scoring import (
    KeywordScorer,
    catalyst_age_ok,
    classify_filing,
    normalize_headline,
)
from scanner.sources.base import RawItem, SourceError
from scanner.sources.edgar import EdgarClient, TickerMap
from scanner.sources.http import HttpClient
from scanner.sources.wires import WireClient

log = logging.getLogger(__name__)


@dataclass
class ScoredCandidate:
    ticker: str
    exchange: str
    source: str
    headline: str
    url: str
    published_at: datetime | None
    catalyst_type: str | None
    headline_score: float
    matched: dict[str, float]
    passed: bool
    drop_stage: str | None
    drop_reason: str | None


@dataclass
class IngestReport:
    run_date: str
    fetched: Counter = field(default_factory=Counter)  # items per source
    new_items: int = 0
    duplicates: int = 0
    unmatched: Counter = field(default_factory=Counter)  # reason -> count
    candidates: list[ScoredCandidate] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    http_requests: int = 0
    cache_hits: int = 0

    @property
    def passed(self) -> list[ScoredCandidate]:
        return sorted((c for c in self.candidates if c.passed), key=lambda c: -c.headline_score)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def _top_keyword(matched: dict[str, float]) -> str | None:
    pos = {k: v for k, v in matched.items() if v > 0}
    return max(pos, key=lambda k: pos[k]) if pos else None


def run_ingest(
    settings: Settings,
    kw: KeywordConfig,
    conn: sqlite3.Connection,
    *,
    edgar: EdgarClient | None,
    wires: WireClient | None,
    now: datetime,
) -> IngestReport:
    src = settings.sources
    sc = settings.scoring
    run_date = now.astimezone(ZoneInfo(settings.schedule.timezone)).date().isoformat()
    report = IngestReport(run_date=run_date)
    scorer = KeywordScorer(kw.keywords)

    # ---- 1. Ingest ------------------------------------------------------- #
    tmap: TickerMap | None = None
    items: list[RawItem] = []
    known_urls = {r[0] for r in conn.execute("SELECT url FROM raw_items")}

    if edgar is not None:
        try:
            tmap = edgar.ticker_map()
        except SourceError as exc:
            report.failures.append(f"SEC ticker map: {exc}")
        for form in src.edgar_forms:
            try:
                got = edgar.current_filings(form, known_urls)
                report.fetched[f"edgar {form}"] += len(got)
                items.extend(got)
            except SourceError as exc:
                report.failures.append(f"EDGAR {form}: {exc}")
    if wires is not None:
        for res in wires.fetch_all():
            report.fetched[res.name] += len(res.items)
            if res.error:
                report.failures.append(f"{res.name}: {res.error}")
            items.extend(res.items)

    # De-dup #1: by URL (against the journal and within this batch).
    fresh: list[RawItem] = []
    for it in items:
        if it.url in known_urls:
            report.duplicates += 1
            continue
        known_urls.add(it.url)
        fresh.append(it)

    release_budget = src.edgar_max_release_fetches
    trigger_forms = {f for t in kw.filing_triggers.values() for f in t.forms}
    seen_headlines: set[tuple[str, str]] = set()

    for it in fresh:
        # ---- 2. Ticker match --------------------------------------------- #
        mres = match_item(it, tmap, src.max_tickers_per_item)
        matches: list[TickerMatch] = mres.matches

        catalyst_type: str | None = None
        score = 0.0
        matched: dict[str, float] = {}
        drop: tuple[str, str] | None = None
        keyword_item = it.source != "edgar"

        # ---- EDGAR specifics: 8-K press release text, filing triggers ---- #
        if it.source == "edgar" and matches:
            if it.form_type == "8-K":
                if not set(it.items) & set(src.edgar_release_items):
                    drop = ("headline_score", f"8-K items {it.items} carry no press release")
                elif release_budget <= 0:
                    drop = ("headline_score", "EX-99.1 fetch budget used up")
                else:
                    release_budget -= 1
                    try:
                        if edgar is not None and edgar.attach_press_release(it):
                            keyword_item = True
                        else:
                            drop = ("headline_score", "no EX-99.1 press release found")
                    except SourceError as exc:
                        drop = ("headline_score", f"EX-99.1 fetch failed: {exc}")
            elif it.form_type in trigger_forms and it.cik is not None and edgar is not None:
                try:
                    hist = edgar.submissions(it.cik)
                    filed_on = (it.published_at or now).date()
                    trig = classify_filing(it.form_type, filed_on, it.accession, hist, kw)
                except SourceError as exc:
                    trig = None
                    drop = ("headline_score", f"filing history fetch failed: {exc}")
                if trig:
                    catalyst_type, score, matched = trig.tag, trig.score, {trig.tag: trig.score}
                elif drop is None:
                    drop = ("headline_score", f"{it.form_type} is not a trigger for this company")
            else:
                drop = ("headline_score", f"form {it.form_type} is not scored")

        # ---- 3. Headline score ------------------------------------------- #
        if keyword_item and drop is None:
            r = scorer.headline_score(it.headline, sc.headline_min_score)
            score, matched = r.score, r.matched
            catalyst_type = _top_keyword(matched)
            if r.has_negative:
                drop = ("headline_score", f"negative keyword in headline: {sorted(k for k, v in matched.items() if v < 0)}")

        if drop is None and not catalyst_age_ok(it.published_at, now, sc.catalyst_max_age_days):
            drop = ("catalyst_age", f"older than {sc.catalyst_max_age_days} days or undated")
        if drop is None and score < sc.headline_min_score:
            drop = ("headline_score", f"score {score:g} < min {sc.headline_min_score:g}")

        # De-dup #2: same ticker + same normalized headline (e.g. wire + Yahoo + 8-K copy).
        norm = normalize_headline(it.headline)
        if keyword_item and matches:
            dup = False
            for m in matches:
                key = (m.ticker, norm)
                if key in seen_headlines or conn.execute(
                    "SELECT 1 FROM raw_items r JOIN item_tickers t ON t.raw_item_id = r.id "
                    "WHERE t.ticker = ? AND r.headline_norm = ? LIMIT 1",
                    key,
                ).fetchone():
                    dup = True
                seen_headlines.add(key)
            if dup:
                report.duplicates += 1
                continue

        # ---- Journal ----------------------------------------------------- #
        report.new_items += 1
        if not matches:
            report.unmatched[mres.reason or "no match"] += 1
        cur = conn.execute(
            "INSERT INTO raw_items (source, url, published_at, ingested_at, run_date, cik, ticker,"
            " form_type, accession, match_note, headline, headline_norm, text_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                it.source, it.url, _iso(it.published_at), _iso(now), run_date,
                str(it.cik) if it.cik is not None else None,
                matches[0].ticker if matches else None,
                it.form_type, it.accession, None if matches else mres.reason,
                it.headline, norm, it.text_hash,
            ),
        )
        raw_id = cur.lastrowid
        for m in matches:
            conn.execute(
                "INSERT INTO item_tickers (raw_item_id, ticker, exchange, method) VALUES (?, ?, ?, ?)",
                (raw_id, m.ticker, m.exchange, m.method),
            )
            cand = ScoredCandidate(
                ticker=m.ticker, exchange=m.exchange, source=it.source, headline=it.headline,
                url=it.url, published_at=it.published_at, catalyst_type=catalyst_type,
                headline_score=score, matched=matched, passed=drop is None,
                drop_stage=drop[0] if drop else None, drop_reason=drop[1] if drop else None,
            )
            report.candidates.append(cand)
            conn.execute(
                "INSERT INTO candidates (run_date, raw_item_id, ticker, catalyst_type, catalyst_at,"
                " matched_keywords, headline_score, final_score, passed, drop_stage, drop_reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_date, raw_id, m.ticker, catalyst_type, _iso(it.published_at),
                    json.dumps(matched), score, score, int(cand.passed), cand.drop_stage, cand.drop_reason,
                ),
            )
    conn.commit()
    return report


# --------------------------------------------------------------------------- #
# Report text
# --------------------------------------------------------------------------- #


def format_report(r: IngestReport) -> str:
    out = [f"# Ingest {r.run_date}", ""]
    out.append("## Sources")
    for name, n in sorted(r.fetched.items()):
        out.append(f"- {name}: {n} items")
    out.append(f"- new: {r.new_items}, duplicates skipped: {r.duplicates}")
    out.append(f"- HTTP requests: {r.http_requests}, cache hits: {r.cache_hits}")
    if r.failures:
        out += ["", "## Source failures"] + [f"- {f}" for f in r.failures]

    out += ["", f"## Passed headline stage ({len(r.passed)})"]
    if r.passed:
        out.append("| Score | Ticker | Exch | Type | Source | Headline |")
        out.append("| ---: | --- | --- | --- | --- | --- |")
        for c in r.passed:
            out.append(
                f"| {c.headline_score:g} | {c.ticker} | {c.exchange or '?'} | {c.catalyst_type or ''} "
                f"| {c.source} | [{c.headline[:110]}]({c.url}) |"
            )
    else:
        out.append("None.")

    scored_fails = sorted(
        (c for c in r.candidates if not c.passed and c.matched),
        key=lambda c: -c.headline_score,
    )
    out += ["", f"## Scored but dropped ({len(scored_fails)})"]
    for c in scored_fails[:40]:
        kws = ", ".join(f"{k} {v:+g}" for k, v in c.matched.items())
        out.append(f"- {c.headline_score:g} {c.ticker}: {c.headline[:100]} — {c.drop_reason} [{kws}]")

    drops = Counter(c.drop_reason.split(":")[0] if c.drop_reason else "" for c in r.candidates if not c.passed)
    out += ["", "## Drop reasons (candidates)"] + [f"- {k}: {v}" for k, v in drops.most_common()]
    out += ["", "## Unmatched items (no ticker)"] + [f"- {k}: {v}" for k, v in r.unmatched.most_common()]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_http(settings: Settings, today) -> HttpClient:  # noqa: ANN001
    ua = os.environ.get(settings.sources.sec_user_agent_env, "").strip() or None
    if ua is None:
        log.warning("%s not set: EDGAR sources will be skipped", settings.sources.sec_user_agent_env)
    return HttpClient(
        generic_user_agent=settings.sources.generic_user_agent,
        sec_user_agent=ua,
        sec_max_requests_per_sec=settings.data.sec_max_requests_per_sec,
        timeout=settings.data.http_timeout_sec,
        max_retries=settings.data.http_max_retries,
        cache_dir=REPO_ROOT / settings.data.cache_dir,
        today=today,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="score and print; write nothing to the journal")
    ap.add_argument("--db", type=Path, help="journal path (default: settings data.db_path)")
    ap.add_argument("--no-edgar", action="store_true")
    ap.add_argument("--no-wires", action="store_true")
    ap.add_argument("--output", type=Path, help="also write the report (markdown) here")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings, kw = load_settings(), load_keywords()
    now = datetime.now(UTC)
    http = build_http(settings, now.date())
    db_path = ":memory:" if args.dry_run else str(args.db or REPO_ROOT / settings.data.db_path)
    conn = init_db(db_path)
    report = run_ingest(
        settings, kw, conn,
        edgar=None if args.no_edgar else EdgarClient(http, settings.sources),
        wires=None if args.no_wires else WireClient(http, settings.sources),
        now=now,
    )
    conn.close()
    report.http_requests, report.cache_hits = http.requests_made, http.cache_hits
    text = format_report(report)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
