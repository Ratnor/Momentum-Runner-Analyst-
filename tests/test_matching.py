from __future__ import annotations

import pytest

from scanner.matching import find_exchange_tickers, match_item, normalize_exchange
from scanner.sources.base import RawItem
from scanner.sources.edgar import parse_ticker_map
from tests.helpers import fixture

TMAP = parse_ticker_map(fixture("company_tickers_exchange.json"))


def wire(headline: str, summary: str = "", tags: list[str] | None = None) -> RawItem:
    return RawItem("globenewswire", "https://x", headline, None, summary, tags or [])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ACME Bio, Inc. (Nasdaq: ACMB) today", [("NASDAQ", "ACMB")]),
        ("(NASDAQ:ACMB)", [("NASDAQ", "ACMB")]),
        ("(Nasdaq CM: ACMB)", [("NASDAQ", "ACMB")]),
        ("(NasdaqGS: ACMB)", [("NASDAQ", "ACMB")]),
        ("(Nasdaq Capital Market: ACMB)", [("NASDAQ", "ACMB")]),
        ("(NYSE American: NOVT)", [("NYSE AMERICAN", "NOVT")]),
        ("(NYSE MKT: NOVT)", [("NYSE AMERICAN", "NOVT")]),
        ("(NYSE: BIG)", [("NYSE", "BIG")]),
        ("(OTCQB: PNKY)", [("OTC", "PNKY")]),
        ("(TSX: MPLM)", [("CANADA", "MPLM")]),
        ("(Nasdaq: ACMB; ACMBW)", [("NASDAQ", "ACMB")]),
        ("Acme (Nasdaq: ACMB) to acquire Beta (NYSE American: BETA)", [("NASDAQ", "ACMB"), ("NYSE AMERICAN", "BETA")]),
        ("ACMB rose on Nasdaq today", []),
        ("time: 10:30 ET", []),
        ("(Nasdaq: acmb)", []),  # tickers must be uppercase
    ],
)
def test_find_exchange_tickers(text, expected):
    assert find_exchange_tickers(text) == expected


def test_normalize_exchange():
    assert normalize_exchange("Nasdaq") == "NASDAQ"
    assert normalize_exchange("nyse american") == "NYSE AMERICAN"
    assert normalize_exchange("NYSE") == "NYSE"
    assert normalize_exchange("OTC") == "OTC"


def test_ticker_map_prefers_common_stock_over_warrant():
    assert TMAP.cik_to_ticker(1111111).ticker == "ACMB"


def test_edgar_item_matched_by_cik():
    item = RawItem("edgar", "u", "8-K - ACME", None, cik=1111111)
    r = match_item(item, TMAP, 3)
    assert [(m.ticker, m.exchange, m.method) for m in r.matches] == [("ACMB", "NASDAQ", "cik")]


def test_edgar_item_unknown_cik_dropped():
    r = match_item(RawItem("edgar", "u", "x", None, cik=42), TMAP, 3)
    assert r.matches == [] and "no ticker" in r.reason


def test_wire_ticker_in_rss_tag():
    r = match_item(wire("Quiet Corp news", tags=["NASDAQ:QUIE"]), TMAP, 3)
    assert [m.ticker for m in r.matches] == ["QUIE"]


def test_wire_ticker_not_in_sec_map_dropped():
    r = match_item(wire("MapleMed news", "(TSX: MPLM)"), TMAP, 3)
    assert r.matches == [] and "not in SEC" in r.reason


def test_wire_without_pattern_dropped():
    r = match_item(wire("Acme rises", "Shares of Acme rose"), TMAP, 3)
    assert r.matches == [] and r.reason == "no exchange:ticker pattern"


def test_roundup_dropped():
    r = match_item(wire("Movers", "(Nasdaq: AAA) (Nasdaq: BBB) (Nasdaq: CCC) (Nasdaq: DDD)"), TMAP, 3)
    assert r.matches == [] and "roundup" in r.reason


def test_merger_release_matches_both_companies():
    r = match_item(wire("Acme (Nasdaq: ACMB) to acquire Zeta (Nasdaq: ZETA)"), TMAP, 3)
    assert [m.ticker for m in r.matches] == ["ACMB", "ZETA"]


def test_without_ticker_map_pattern_is_trusted():
    r = match_item(wire("MapleMed", "(TSX: MPLM)"), None, 3)
    assert [m.ticker for m in r.matches] == ["MPLM"]
