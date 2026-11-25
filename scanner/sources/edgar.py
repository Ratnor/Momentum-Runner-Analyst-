"""EDGAR adapter: latest-filings Atom feed, CIK<->ticker map, filing history, 8-K press releases.

Parsers are pure functions (text in, objects out) so they are tested on saved
fixtures; ``EdgarClient`` only adds fetching through the shared HttpClient.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import quote, urljoin

import feedparser

from scanner.config import SourcesConfig
from scanner.sources.base import RawItem, SourceError, html_to_text, to_utc
from scanner.sources.http import HttpClient

log = logging.getLogger(__name__)

SEC_BASE = "https://www.sec.gov"


# --------------------------------------------------------------------------- #
# CIK <-> ticker map (company_tickers_exchange.json)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TickerInfo:
    cik: int
    ticker: str
    name: str
    exchange: str  # as SEC reports it: 'Nasdaq', 'NYSE', 'OTC', 'CBOE', '' ...


@dataclass
class TickerMap:
    by_ticker: dict[str, TickerInfo]
    by_cik: dict[int, TickerInfo]  # primary (common-stock) ticker per company

    def cik_to_ticker(self, cik: int) -> TickerInfo | None:
        return self.by_cik.get(cik)

    def lookup(self, ticker: str) -> TickerInfo | None:
        return self.by_ticker.get(ticker.upper())

    def __len__(self) -> int:
        return len(self.by_ticker)


_LISTED = {"nasdaq", "nyse"}


def _primary_rank(info: TickerInfo) -> tuple[int, int, str]:
    # Prefer listed exchanges, then the shortest symbol (common stock, not
    # warrants "ABCDW" / units "ABCDU"), then alphabetical for stability.
    return (0 if info.exchange.lower() in _LISTED else 1, len(info.ticker), info.ticker)


def parse_ticker_map(text: str) -> TickerMap:
    """Parse SEC company_tickers_exchange.json ({"fields": [...], "data": [[...], ...]})."""
    try:
        doc = json.loads(text)
        idx = {name: i for i, name in enumerate(doc["fields"])}
        rows = doc["data"]
        ci, ti, ni, ei = idx["cik"], idx["ticker"], idx["name"], idx["exchange"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError(f"unexpected ticker map format: {exc}") from exc

    by_ticker: dict[str, TickerInfo] = {}
    by_cik: dict[int, TickerInfo] = {}
    for row in rows:
        if not row[ti]:
            continue
        info = TickerInfo(int(row[ci]), str(row[ti]).upper(), str(row[ni]), str(row[ei] or ""))
        by_ticker.setdefault(info.ticker, info)
        cur = by_cik.get(info.cik)
        if cur is None or _primary_rank(info) < _primary_rank(cur):
            by_cik[info.cik] = info
    return TickerMap(by_ticker, by_cik)


# --------------------------------------------------------------------------- #
# Latest filings Atom feed (browse-edgar?action=getcurrent)
# --------------------------------------------------------------------------- #

_TITLE_RE = re.compile(r"^(?P<form>.+?) - (?P<company>.+) \((?P<cik>\d{1,10})\) \((?P<role>[^)]+)\)\s*$")
_ACC_RE = re.compile(r"\b(\d{10}-\d{2}-\d{6})\b")
_ITEM_RE = re.compile(r"Item\s+(\d{1,2}\.\d{2})", re.IGNORECASE)


def parse_current_atom(text: str) -> list[RawItem]:
    """Parse one page of the EDGAR latest-filings Atom feed.

    The feed lists one entry per (filing, role). For 13D-style filings the
    *Subject* company is the one we care about; otherwise the *Filer*. Other
    roles (Reporting, Filed by) are dropped. One RawItem per accession.
    """
    feed = feedparser.parse(text)
    if feed.bozo and not feed.entries:
        raise SourceError(f"EDGAR Atom parse error: {feed.bozo_exception}")

    chosen: dict[str, tuple[int, RawItem]] = {}
    for e in feed.entries:
        m = _TITLE_RE.match(e.get("title", ""))
        if not m:
            continue
        role = m["role"].strip().lower()
        if role not in ("filer", "subject"):
            continue
        summary_html = e.get("summary", "")
        acc_m = _ACC_RE.search(summary_html) or _ACC_RE.search(e.get("id", ""))
        if not acc_m:
            continue
        accession = acc_m.group(1)
        tags = [t.get("term", "") for t in e.get("tags", []) if t.get("term")]
        form = (tags[0] if tags else m["form"]).strip()
        updated = e.get("updated_parsed") or e.get("published_parsed")
        published = to_utc(datetime(*updated[:6])) if updated else None
        summary = html_to_text(summary_html)
        item = RawItem(
            source="edgar",
            url=e.get("link", ""),
            headline=f"{form} - {m['company'].strip()}",
            published_at=published,
            summary=summary,
            cik=int(m["cik"]),
            company=m["company"].strip(),
            form_type=form,
            accession=accession,
            items=sorted(set(_ITEM_RE.findall(summary_html))),
        )
        rank = 0 if role == "subject" else 1
        if accession not in chosen or rank < chosen[accession][0]:
            chosen[accession] = (rank, item)
    return [item for _, item in chosen.values()]


# --------------------------------------------------------------------------- #
# Filing history (data.sec.gov/submissions/CIK##########.json)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Filing:
    form: str
    filing_date: date
    accession: str


@dataclass
class FilingHistory:
    cik: int
    name: str
    sic: int | None
    tickers: list[str]
    exchanges: list[str]
    filings: list[Filing] = field(default_factory=list)  # newest first


def parse_submissions(text: str) -> FilingHistory:
    try:
        doc = json.loads(text)
        recent = doc["filings"]["recent"]
        forms, dates, accs = recent["form"], recent["filingDate"], recent["accessionNumber"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError(f"unexpected submissions format: {exc}") from exc
    sic_raw = str(doc.get("sic") or "").strip()
    filings = [
        Filing(f, date.fromisoformat(d), a) for f, d, a in zip(forms, dates, accs, strict=False) if d
    ]
    filings.sort(key=lambda f: f.filing_date, reverse=True)
    return FilingHistory(
        cik=int(doc["cik"]),
        name=str(doc.get("name", "")),
        sic=int(sic_raw) if sic_raw.isdigit() else None,
        tickers=[str(t) for t in doc.get("tickers", [])],
        exchanges=[str(x or "") for x in doc.get("exchanges", [])],
        filings=filings,
    )


# --------------------------------------------------------------------------- #
# 8-K press release (EX-99.1)
# --------------------------------------------------------------------------- #

_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_EX99_RE = re.compile(r"^EX-99(?:\.0?(\d+))?$", re.IGNORECASE)


def find_press_release_url(index_html: str, index_url: str) -> str | None:
    """From a filing's -index.htm page, return the EX-99.1 document URL (or first EX-99.x)."""
    best: tuple[int, str] | None = None
    for row in _ROW_RE.findall(index_html):
        cells = [html_to_text(c).strip() for c in _CELL_RE.findall(row)]
        href = _HREF_RE.search(row)
        if not href:
            continue
        for cell in cells:
            m = _EX99_RE.match(cell)
            if m:
                n = int(m.group(1) or 1)
                link = href.group(1).replace("/ix?doc=", "")
                if best is None or n < best[0]:
                    best = (n, urljoin(index_url, link))
                break
    return best[1] if best else None


_SKIP_LINE = re.compile(
    r"^(exhibit|ex-)\s*99|for immediate release|^(press|news) release$|^source:|^contact",
    re.IGNORECASE,
)
_DATE_ONLY = re.compile(
    r"^[A-Z][a-z]+\.? \d{1,2}, \d{4}$|^\d{1,2}/\d{1,2}/\d{2,4}$", re.IGNORECASE
)


def extract_release_headline(text: str) -> str | None:
    """Best guess at a press release title: first line of 4-40 words after the boilerplate."""
    for line in text.splitlines()[:25]:
        line = line.strip()
        if not line or _SKIP_LINE.search(line) or _DATE_ONLY.match(line):
            continue
        n = len(line.split())
        if 4 <= n <= 40:
            return line
    return None


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class EdgarClient:
    def __init__(self, http: HttpClient, cfg: SourcesConfig) -> None:
        self.http = http
        self.cfg = cfg

    def ticker_map(self) -> TickerMap:
        return parse_ticker_map(self.http.get_text(self.cfg.ticker_map_url))

    def current_filings(self, form: str, known_urls: Iterable[str] = ()) -> list[RawItem]:
        """Newest filings of one form type, paging until a page holds nothing new."""
        known = set(known_urls)
        out: list[RawItem] = []
        seen: set[str] = set()
        for page in range(self.cfg.edgar_max_pages):
            url = self.cfg.edgar_current_url.format(
                form=quote(form), start=page * self.cfg.edgar_page_size, count=self.cfg.edgar_page_size
            )
            # The live feed changes minute to minute: never serve it from cache.
            items = parse_current_atom(self.http.get_text(url, use_cache=False))
            new = [i for i in items if i.url not in known and i.accession not in seen]
            for i in new:
                seen.add(i.accession or i.url)
            # The feed's type filter is a prefix match (8-K also returns 8-K/A); keep exact forms.
            out.extend(i for i in new if i.form_type == form)
            if len(items) < self.cfg.edgar_page_size or not new:
                break
        return out

    def submissions(self, cik: int) -> FilingHistory:
        return parse_submissions(self.http.get_text(self.cfg.submissions_url.format(cik=cik)))

    def attach_press_release(self, item: RawItem) -> bool:
        """For an 8-K, fetch its EX-99.1 and set item.headline/body. Returns True on success."""
        index_html = self.http.get_text(item.url)
        doc_url = find_press_release_url(index_html, item.url or SEC_BASE)
        if not doc_url:
            return False
        body = html_to_text(self.http.get_text(doc_url))
        headline = extract_release_headline(body)
        if not headline:
            return False
        item.headline = headline
        item.body = body
        return True
