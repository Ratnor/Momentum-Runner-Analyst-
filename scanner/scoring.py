"""Catalyst scoring: weighted keyword table + filing-based triggers.

Rules (from the spec):
- Case-insensitive match on whole words and phrases; each keyword counts once per item.
- Headline score = sum of matched weights in the headline.
- Body score = sum of matched weights in the body x body_weight.
- Any negative keyword in the headline caps the headline at headline_min_score - 1.
- Catalyst must be at most catalyst_max_age_days old.
- Final score = headline + body - risk penalties (penalties arrive in phase 3).

Everything here is a pure function so the backtest can reuse it unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from scanner.config import KeywordConfig
from scanner.sources.edgar import FilingHistory


def _phrase_regex(phrase: str) -> re.Pattern[str]:
    # Words may be separated by spaces or hyphens ("at-the-market" == "at the market").
    words = [re.escape(w) for w in re.split(r"[\s\-]+", phrase.strip()) if w]
    body = r"[\s\-]+".join(words)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


@dataclass(frozen=True)
class ScoreResult:
    score: float
    matched: dict[str, float] = field(default_factory=dict)
    capped: bool = False  # headline only: a negative keyword capped the score

    @property
    def has_negative(self) -> bool:
        return any(w < 0 for w in self.matched.values())


class KeywordScorer:
    def __init__(self, keywords: dict[str, float]) -> None:
        self._rules = [(phrase, float(w), _phrase_regex(phrase)) for phrase, w in keywords.items()]

    def score(self, text: str) -> ScoreResult:
        """Raw sum of matched weights; each phrase counts at most once."""
        matched = {phrase: w for phrase, w, rx in self._rules if text and rx.search(text)}
        return ScoreResult(sum(matched.values()), matched)

    def headline_score(self, headline: str, headline_min_score: float) -> ScoreResult:
        r = self.score(headline)
        if r.has_negative and r.score > headline_min_score - 1:
            return ScoreResult(headline_min_score - 1, r.matched, capped=True)
        return r

    def body_score(self, body: str, body_weight: float) -> ScoreResult:
        r = self.score(body)
        return ScoreResult(r.score * body_weight, r.matched)


def final_score(headline: float, body: float, risk_penalty: float = 0.0) -> float:
    """``risk_penalty`` is the signed sum of risk-flag adjustments (penalties are negative)."""
    return headline + body + risk_penalty


def catalyst_age_ok(published_at: datetime | None, now: datetime, max_age_days: int) -> bool:
    if published_at is None:
        return False
    return now - published_at <= timedelta(days=max_age_days)


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_headline(text: str) -> str:
    """Lowercase, punctuation stripped, whitespace collapsed - for cross-source de-dup."""
    return _NON_ALNUM.sub(" ", text.lower()).strip()


# --------------------------------------------------------------------------- #
# Filing-based triggers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FilingTriggerResult:
    tag: str  # 'ipo_low_float' | 'refiled_s1' | 'dilution_on_candidate'
    score: float
    reason: str


def classify_filing(
    form: str,
    filed_on: date,
    accession: str | None,
    history: FilingHistory,
    kw: KeywordConfig,
) -> FilingTriggerResult | None:
    """Decide whether a filing is an IPO trigger, a dilution negative, or neither.

    Dilution filings are triggers only for fresh IPOs, never for existing
    listings. "Existing listing" = a periodic report (10-K/10-Q...) was filed
    before this filing. The float limit for ``ipo_low_float`` is enforced by
    the float hard filter in phase 3.
    """
    rules = kw.filing_rules
    trig = kw.filing_triggers
    prior = [f for f in history.filings if f.filing_date <= filed_on and f.accession != accession]
    window_start = filed_on - timedelta(days=rules.ipo_lookback_days)
    recent = [f for f in prior if f.filing_date >= window_start]
    existing = any(f.form in rules.periodic_report_forms for f in prior)

    if not existing:
        if form in trig["ipo_low_float"].forms and any(
            f.form in rules.registration_forms for f in recent
        ):
            t = trig["ipo_low_float"]
            return FilingTriggerResult("ipo_low_float", t.score, f"{form} pricing after S-1, no prior periodic reports")
        if form in trig["refiled_s1"].forms and any(f.form in rules.withdrawal_forms for f in recent):
            t = trig["refiled_s1"]
            return FilingTriggerResult("refiled_s1", t.score, f"{form} re-filed after RW withdrawal")
        return None

    if form in trig["dilution_on_candidate"].forms:
        t = trig["dilution_on_candidate"]
        return FilingTriggerResult("dilution_on_candidate", t.score, f"{form} on an existing listing")
    return None
