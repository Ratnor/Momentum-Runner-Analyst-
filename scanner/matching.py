"""Map an ingested item to ticker(s).

- EDGAR items: by CIK through the SEC ticker map.
- Wire items: by the "Exchange: TICKER" pattern in the headline, teaser or RSS
  tags, e.g. "(Nasdaq: ABCD)", "(NYSE American: XYZ)", "NASDAQ:ABCD".
  Wire tickers are confirmed against the SEC ticker map when it is available.

No confident match -> empty result plus a reason; the caller drops the item.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from scanner.sources.base import RawItem
from scanner.sources.edgar import TickerMap

_EXCHANGE = (
    r"(?P<ex>(?i:"
    r"NYSE\s+American|NYSE\s+Amer|NYSE\s+MKT|NYSE\s+Arca|NYSE"
    r"|Nasdaq(?:\s*-?\s*(?:GS|GM|CM|Global\s+Select(?:\s+Market)?|Global(?:\s+Market)?"
    r"|Capital(?:\s+Market)?))?"
    r"|OTCQB|OTCQX|OTC\s*Pink|OTCPK|OTC(?:\s+Markets)?"
    r"|TSX\s+Venture|TSXV|TSX|CSE|Cboe(?:\s+[A-Za-z]+)?"
    r"))"
)
# Ticker is uppercase letters, optional class suffix (".A"); must not run on into more letters.
TICKER_PATTERN = re.compile(_EXCHANGE + r"\s*:\s*(?P<tk>[A-Z]{1,5}(?:\.[A-Z])?)(?![A-Za-z])")


def normalize_exchange(raw: str) -> str:
    """Map exchange spellings to the names used in settings.filters.allowed_exchanges."""
    s = re.sub(r"\s+", " ", raw.strip()).upper()
    if s.startswith("NASDAQ"):
        return "NASDAQ"
    if s.startswith(("NYSE AMER", "NYSE MKT")):
        return "NYSE AMERICAN"
    if s.startswith("NYSE ARCA"):
        return "NYSE ARCA"
    if s.startswith("NYSE"):
        return "NYSE"
    if s.startswith("OTC"):
        return "OTC"
    if s.startswith(("TSX", "CSE")):
        return "CANADA"
    if s.startswith("CBOE"):
        return "CBOE"
    return s


@dataclass(frozen=True)
class TickerMatch:
    ticker: str
    exchange: str  # normalized; '' when unknown
    method: str  # 'cik' | 'pattern'


@dataclass(frozen=True)
class MatchResult:
    matches: list[TickerMatch]
    reason: str | None = None  # why there is no confident match


def find_exchange_tickers(text: str) -> list[tuple[str, str]]:
    """All (normalized exchange, ticker) pairs in text, in order of first appearance."""
    seen: dict[str, str] = {}
    for m in TICKER_PATTERN.finditer(text):
        seen.setdefault(m["tk"], normalize_exchange(m["ex"]))
    return [(ex, tk) for tk, ex in seen.items()]


def match_item(item: RawItem, tmap: TickerMap | None, max_tickers: int) -> MatchResult:
    if item.cik is not None:
        if tmap is None:
            return MatchResult([], "ticker map unavailable")
        info = tmap.cik_to_ticker(item.cik)
        if info is None:
            return MatchResult([], f"CIK {item.cik} has no ticker")
        return MatchResult([TickerMatch(info.ticker, normalize_exchange(info.exchange), "cik")])

    text = "\n".join([item.headline, item.summary, *item.tags])
    found = find_exchange_tickers(text)
    if not found:
        return MatchResult([], "no exchange:ticker pattern")
    if tmap is not None:
        found = [(ex, tk) for ex, tk in found if tmap.lookup(tk) is not None]
        if not found:
            return MatchResult([], "ticker(s) not in SEC ticker map")
    if len(found) > max_tickers:
        return MatchResult([], f"{len(found)} tickers mentioned (roundup, not a catalyst)")
    return MatchResult([TickerMatch(tk, ex, "pattern") for ex, tk in found])
