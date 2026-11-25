from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import yaml

from scanner.config import load_keywords, load_settings
from scanner.scoring import (
    KeywordScorer,
    catalyst_age_ok,
    classify_filing,
    final_score,
    normalize_headline,
)
from scanner.sources.edgar import Filing, FilingHistory
from tests.helpers import FIXTURES

KW = load_keywords()
S = load_settings()
SCORER = KeywordScorer(KW.keywords)
CASES = yaml.safe_load((FIXTURES / "headlines.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[c["headline"][:50] for c in CASES])
def test_fixture_headlines_score_exactly_as_keyword_table(case):
    r = SCORER.headline_score(case["headline"], S.scoring.headline_min_score)
    assert r.score == case["score"]
    assert sorted(r.matched) == sorted(case["matched"])
    assert r.capped == case.get("capped", False)
    # Weights reported are exactly the table's weights.
    for phrase, w in r.matched.items():
        assert w == KW.keywords[phrase]


def test_every_keyword_in_table_is_matchable_alone():
    for phrase, weight in KW.keywords.items():
        r = SCORER.score(f"Company news: {phrase} announced")
        assert phrase in r.matched, phrase
        assert r.matched[phrase] == weight


def test_negative_headline_can_never_pass():
    for phrase, w in KW.keywords.items():
        if w >= 0:
            continue
        big = "FDA approval Phase 3 PDUFA date acquisition definitive agreement " + phrase
        r = SCORER.headline_score(big, S.scoring.headline_min_score)
        assert r.score < S.scoring.headline_min_score, phrase


def test_body_score_uses_body_weight():
    body = "The FDA approval follows the Phase 3 trial. FDA approval again."
    r = SCORER.body_score(body, 0.4)
    assert r.score == pytest.approx((10 + 6) * 0.4)


def test_final_score():
    assert final_score(6, 6.4, -4) == pytest.approx(8.4)


def test_catalyst_age():
    now = datetime(2026, 11, 25, 22, 0, tzinfo=UTC)
    assert catalyst_age_ok(now - timedelta(days=10), now, 10)
    assert not catalyst_age_ok(now - timedelta(days=10, seconds=1), now, 10)
    assert not catalyst_age_ok(None, now, 10)


def test_normalize_headline():
    assert normalize_headline("ACME Bio Announces: Positive Phase-3 Results!") == normalize_headline(
        "acme bio announces positive phase 3   results"
    )


# ---- filing triggers ------------------------------------------------------ #


def hist(*filings: tuple[str, str]) -> FilingHistory:
    return FilingHistory(
        cik=1, name="X", sic=2834, tickers=["X"], exchanges=["Nasdaq"],
        filings=[Filing(f, date.fromisoformat(d), f"acc-{i}") for i, (f, d) in enumerate(filings)],
    )


D = date(2026, 11, 25)


def test_ipo_pricing_after_s1_is_trigger():
    r = classify_filing("424B4", D, "new", hist(("S-1", "2026-09-01")), KW)
    assert r and r.tag == "ipo_low_float" and r.score == 6


def test_refiled_s1_after_rw_is_trigger():
    r = classify_filing("S-1", D, "new", hist(("RW", "2026-06-01"), ("S-1", "2026-02-01")), KW)
    assert r and r.tag == "refiled_s1" and r.score == 3


def test_first_s1_alone_is_not_a_trigger():
    assert classify_filing("S-1", D, "new", hist(), KW) is None


@pytest.mark.parametrize("form", ["S-1", "S-3", "424B5", "424B4"])
def test_dilution_on_existing_listing_is_negative(form):
    h = hist(("10-Q", "2026-11-10"), ("S-1", "2026-01-01"), ("RW", "2026-02-01"))
    r = classify_filing(form, D, "new", h, KW)
    assert r and r.tag == "dilution_on_candidate" and r.score == -8


def test_old_s1_outside_lookback_does_not_trigger():
    assert classify_filing("424B4", D, "new", hist(("S-1", "2024-01-01")), KW) is None


def test_the_filing_itself_is_not_its_own_history():
    h = FilingHistory(1, "X", None, [], [], [Filing("S-1", D, "same")])
    assert classify_filing("424B4", D, "same", h, KW) is None
