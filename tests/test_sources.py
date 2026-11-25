from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import requests

from scanner.config import load_settings
from scanner.sources.base import html_to_text
from scanner.sources.edgar import (
    EdgarClient,
    extract_release_headline,
    find_press_release_url,
    parse_current_atom,
    parse_submissions,
    parse_ticker_map,
)
from scanner.sources.http import HttpClient, SourceError
from scanner.sources.wires import WireClient, parse_rss
from tests.helpers import FakeHttp, fixture, standard_routes

S = load_settings()


# ---- EDGAR parsers --------------------------------------------------------- #


def test_parse_current_atom_8k():
    items = {i.accession: i for i in parse_current_atom(fixture("edgar_current_8k.atom"))}
    acme = items["0001111111-26-000042"]
    assert acme.cik == 1111111
    assert acme.form_type == "8-K"
    assert acme.company == "ACME BIO INC"
    assert acme.items == ["8.01", "9.01"]
    assert acme.published_at == datetime(2026, 11, 25, 21, 5, 12, tzinfo=UTC)
    assert acme.url.endswith("0001111111-26-000042-index.htm")
    assert items["0002222222-26-000007"].items == ["5.02"]
    assert items["0001111111-26-000043"].form_type == "8-K/A"


def test_parse_current_atom_prefers_subject_role():
    items = parse_current_atom(fixture("edgar_current_13d.atom"))
    assert len(items) == 1
    assert items[0].cik == 6666666 and items[0].company == "ZETAGEN INC"


def test_parse_current_atom_empty_feed():
    assert parse_current_atom(fixture("edgar_current_empty.atom")) == []


def test_parse_ticker_map_bad_format():
    with pytest.raises(SourceError):
        parse_ticker_map('{"nope": 1}')


def test_parse_submissions():
    h = parse_submissions(fixture("submissions_4444444.json"))
    assert h.cik == 4444444 and h.sic == 2834 and h.tickers == ["PIPO"]
    assert [f.form for f in h.filings] == ["424B4", "S-1/A", "S-1"]
    assert h.filings[-1].filing_date == date(2026, 9, 1)


def test_find_press_release_url():
    base = "https://www.sec.gov/Archives/edgar/data/1111111/000111111126000042/0001111111-26-000042-index.htm"
    url = find_press_release_url(fixture("acme_8k_index.htm"), base)
    assert url == "https://www.sec.gov/Archives/edgar/data/1111111/000111111126000042/ex99-1.htm"


def test_extract_release_headline_skips_boilerplate():
    text = html_to_text(fixture("acme_ex99_1.htm"))
    assert text.startswith("Exhibit 99.1")
    assert extract_release_headline(text) == "ACME Bio Announces Positive Phase 3 Topline Results for ACM-101"


def test_html_to_text_drops_style_and_tags():
    assert html_to_text("<style>p{x}</style><p>Hello&nbsp;<b>world</b></p><p>Two</p>") == "Hello world\nTwo"


def test_edgar_client_filters_exact_form_and_known_urls():
    fake = FakeHttp(standard_routes(S))
    client = EdgarClient(fake, S.sources)
    got = client.current_filings("8-K")
    assert {i.form_type for i in got} == {"8-K"}
    assert len(got) == 3
    known = {got[0].url}
    assert len(client.current_filings("8-K", known)) == 2


def test_edgar_client_attach_press_release():
    fake = FakeHttp(standard_routes(S))
    client = EdgarClient(fake, S.sources)
    acme = next(i for i in client.current_filings("8-K") if i.cik == 1111111)
    assert client.attach_press_release(acme)
    assert acme.headline.startswith("ACME Bio Announces Positive Phase 3")
    assert "(Nasdaq: ACMB)" in acme.body


# ---- wires ----------------------------------------------------------------- #


def test_parse_rss_globenewswire():
    items = parse_rss(fixture("globenewswire.rss"), "globenewswire")
    assert len(items) == 6
    nova = items[0]
    assert nova.headline == "NovaTech Receives First Commercial Order for Its Edge AI Module"
    assert nova.published_at == datetime(2026, 11, 25, 13, 30, tzinfo=UTC)
    assert "NYSE American: NOVT" in nova.summary
    assert "NYSE American:NOVT" in nova.tags
    assert "<p>" not in nova.summary


def test_parse_rss_garbage_raises():
    with pytest.raises(SourceError):
        parse_rss("this is not xml at all <<<", "x")


def test_wire_client_dead_feeds_fail_soft():
    results = {r.name: r for r in WireClient(FakeHttp(standard_routes(S)), S.sources).fetch_all()}
    assert len(results["globenewswire"].items) == 6
    assert len(results["prnewswire"].items) == 2
    assert results["businesswire"].error and results["businesswire"].items == []
    assert results["accesswire"].error


def test_yahoo_headlines():
    url = S.sources.yahoo_headline_url.format(ticker="ACMB")
    items = WireClient(FakeHttp({url: "yahoo_acmb.rss"}), S.sources).yahoo_headlines("ACMB")
    assert items[0].source == "yahoo" and "Phase 3" in items[0].headline


# ---- HTTP client ----------------------------------------------------------- #


class FakeResponse:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code, self.text = status, text


class FakeSession:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers, timeout):  # noqa: ANN001
        self.calls.append((url, headers))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make_client(session, tmp_path: Path | None = None, sec_ua: str | None = "Test Bot test@example.org"):
    sleeps: list[float] = []
    clock = iter(float(i) * 0.01 for i in range(1000))
    c = HttpClient(
        generic_user_agent="Generic/1.0",
        sec_user_agent=sec_ua,
        sec_max_requests_per_sec=8,
        timeout=5,
        max_retries=2,
        cache_dir=tmp_path,
        today=date(2026, 11, 25),
        session=session,
        sleep=sleeps.append,
        clock=lambda: next(clock),
    )
    return c, sleeps


def test_http_retries_transient_then_succeeds():
    sess = FakeSession([FakeResponse(503), requests.ConnectionError("boom"), FakeResponse(200, "ok")])
    c, sleeps = make_client(sess)
    assert c.get_text("https://example.com/a") == "ok"
    assert len(sess.calls) == 3
    assert [s for s in sleeps if s >= 1] == [2.0, 4.0]  # exponential backoff


def test_http_gives_up_after_retries():
    c, _ = make_client(FakeSession([FakeResponse(500)] * 3))
    with pytest.raises(SourceError, match="3 attempts"):
        c.get_text("https://example.com/a")


def test_http_404_is_not_retried():
    sess = FakeSession([FakeResponse(404)])
    c, _ = make_client(sess)
    with pytest.raises(SourceError, match="404"):
        c.get_text("https://example.com/a")
    assert len(sess.calls) == 1


def test_http_user_agents():
    sess = FakeSession([FakeResponse(200, "a"), FakeResponse(200, "b")])
    c, _ = make_client(sess)
    c.get_text("https://data.sec.gov/x")
    c.get_text("https://www.globenewswire.com/x")
    assert sess.calls[0][1]["User-Agent"] == "Test Bot test@example.org"
    assert sess.calls[1][1]["User-Agent"] == "Generic/1.0"


def test_http_sec_requires_user_agent():
    sess = FakeSession([])
    c, _ = make_client(sess, sec_ua=None)
    with pytest.raises(SourceError, match="User-Agent"):
        c.get_text("https://www.sec.gov/x")
    assert sess.calls == []


def test_http_sec_rate_limited():
    sess = FakeSession([FakeResponse(200, "a"), FakeResponse(200, "b")])
    c, sleeps = make_client(sess)
    c.get_text("https://www.sec.gov/1")
    c.get_text("https://www.sec.gov/2")
    # 8 req/s -> 0.125 s apart; the fake clock advanced only ~0.01 s, so it waits.
    assert any(0.1 < s <= 0.125 for s in sleeps)


def test_http_daily_cache(tmp_path):
    sess = FakeSession([FakeResponse(200, "cached body")])
    c, _ = make_client(sess, tmp_path)
    assert c.get_text("https://example.com/a") == "cached body"
    assert c.get_text("https://example.com/a") == "cached body"
    assert len(sess.calls) == 1 and c.cache_hits == 1
    assert (tmp_path / "2026-11-25").is_dir()


def test_http_cache_bypass(tmp_path):
    sess = FakeSession([FakeResponse(200, "v1"), FakeResponse(200, "v2")])
    c, _ = make_client(sess, tmp_path)
    c.get_text("https://example.com/feed", use_cache=False)
    assert c.get_text("https://example.com/feed", use_cache=False) == "v2"
