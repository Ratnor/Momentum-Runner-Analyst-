from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from scanner.config import load_keywords, load_settings
from scanner.ingest import format_report, run_ingest
from scanner.journal import init_db
from scanner.sources.edgar import EdgarClient
from scanner.sources.wires import WireClient
from tests.helpers import FakeHttp, standard_routes

S = load_settings()
KW = load_keywords()
NOW = datetime(2026, 11, 25, 22, 30, tzinfo=UTC)  # 17:30 ET


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "scanner.db")
    yield c
    c.close()


def run(conn, *, sec: bool = True):
    fake = FakeHttp(standard_routes(S, sec=sec))
    return run_ingest(
        S, KW, conn, edgar=EdgarClient(fake, S.sources), wires=WireClient(fake, S.sources), now=NOW
    )


def by_ticker(report):
    return {c.ticker: c for c in report.candidates}


def test_end_to_end_scoring_and_drops(conn):
    r = run(conn)
    c = by_ticker(r)

    # Passed the headline stage
    assert sorted((x.ticker, x.headline_score) for x in r.passed) == [
        ("ACMB", 6.0), ("NOVT", 5.0), ("PIPO", 6.0), ("PNKY", 10.0), ("QUIE", 10.0)
    ]
    assert c["ACMB"].source == "edgar"
    assert c["ACMB"].headline.startswith("ACME Bio Announces Positive Phase 3")
    assert c["PIPO"].catalyst_type == "ipo_low_float"
    assert c["NOVT"].exchange == "NYSE AMERICAN"
    assert c["PNKY"].exchange == "OTC"  # dropped later by the exchange hard filter (phase 3)

    # Dropped, each with a recorded reason
    assert c["OLDC"].catalyst_type == "dilution_on_candidate" and not c["OLDC"].passed
    assert "negative keyword" in c["ZETA"].drop_reason
    assert c["OLDN"].drop_stage == "catalyst_age"
    quie = {x.source: x for x in r.candidates if x.ticker == "QUIE"}
    assert "carry no press release" in quie["edgar"].drop_reason  # 8-K item 5.02 only
    assert quie["prnewswire"].passed  # its acquisition release
    assert not any(x.ticker in {"AAA", "MPLM"} for x in r.candidates)

    # Fail-soft: dead feeds reported, run continued
    assert any("businesswire" in f for f in r.failures)
    assert any("accesswire" in f for f in r.failures)

    # The wire copy of the ACME release is a duplicate of the 8-K's EX-99.1
    assert sum(1 for x in r.candidates if x.ticker == "ACMB") == 1
    assert r.unmatched["CIK 1234567 has no ticker"] == 1
    assert any("roundup" in k for k in r.unmatched)


def test_everything_is_journaled_including_rejects(conn):
    r = run(conn)
    rows = conn.execute("SELECT * FROM candidates").fetchall()
    assert len(rows) == len(r.candidates)
    for row in rows:
        if row["passed"]:
            assert row["drop_reason"] is None
        else:
            assert row["drop_stage"] and row["drop_reason"]
    acmb = conn.execute("SELECT * FROM candidates WHERE ticker='ACMB'").fetchone()
    assert json.loads(acmb["matched_keywords"]) == {"Phase 3": 6.0}
    unmatched = conn.execute("SELECT match_note FROM raw_items WHERE ticker IS NULL").fetchall()
    assert {u[0] for u in unmatched} >= {"CIK 1234567 has no ticker", "ticker(s) not in SEC ticker map"}
    assert conn.execute("SELECT COUNT(*) FROM item_tickers").fetchone()[0] == len(r.candidates)


def test_second_run_ingests_nothing_new(conn):
    run(conn)
    before = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    r2 = run(conn)
    assert r2.candidates == []
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == before


def test_without_sec_access_wires_still_run(conn):
    r = run(conn, sec=False)
    assert any("ticker map" in f for f in r.failures)
    assert any("EDGAR 8-K" in f for f in r.failures)
    assert {"NOVT", "QUIE"} <= {x.ticker for x in r.passed}


def test_report_lists_scored_items(conn):
    text = format_report(run(conn))
    assert "## Passed headline stage (5)" in text
    assert "| 10 | QUIE |" in text
    assert "businesswire" in text
