# Momentum Runner Scanner

Finds low-float US micro-cap momentum runners (biotech and pre-revenue tech)
from fresh catalysts, sizes positions automatically, and paper-trades them so
the strategy can be judged on 6–8 weeks of forward results before any real money.

## Build status

| Phase | Status |
| --- | --- |
| 1. Skeleton: config loading, SQLite schema, `Broker` interface, CI | ✅ done |
| 2. Ingest + scoring: EDGAR + RSS adapters, ticker matching, keyword scorer | ✅ built — live check via *Ingest dry run* workflow |
| 3. Filters + risk flags | not started |
| 4. Sizing + paper broker | not started |
| 5. Nightly run + alerts | not started |
| 6. Backtest | not started |

## Layout

```
config/settings.yaml      every threshold, sizing rule and schedule value
config/keywords.yaml      catalyst keyword weights and filing triggers
scanner/config.py         loads + validates both YAML files into read-only objects
scanner/schema.sql        journal tables (raw_items, candidates, candidate_filters,
                          tickets, positions, fills, equity)
scanner/journal.py        creates data/scanner.db from the schema; connections
scanner/models.py         DailyBar, OrderTicket, Fill, Position
scanner/brokers/base.py   the Broker interface every broker implements
scanner/sources/http.py  one HTTP client: User-Agent, SEC rate limit, retries, daily cache
scanner/sources/edgar.py  latest filings, CIK<->ticker map, filing history, 8-K press releases
scanner/sources/wires.py  newswire RSS + Yahoo per-ticker headlines
scanner/matching.py       item -> ticker(s): by CIK (EDGAR) or "(Nasdaq: ABCD)" (wires)
scanner/scoring.py        keyword scorer, negative cap, catalyst age, filing triggers
scanner/ingest.py         stages 1-3 end to end; journals every item and candidate
scanner/strategies/       entry/exit strategies (phase 4)
backtest/                 exit-rule comparison (phase 6)
tests/                    pytest, no network calls
```

## Run locally

```bash
pip install -e ".[dev]"
pytest                    # run the tests
python -m scanner.journal # create/upgrade data/scanner.db
SEC_USER_AGENT="Your Name you@example.com" python -m scanner.ingest --dry-run
```

## GitHub secrets

| Secret | Needed for |
| --- | --- |
| `SEC_USER_AGENT` | All EDGAR sources. SEC requires a name and email, e.g. `Jane Doe jane@example.com`. Without it EDGAR is skipped and only newswires run. |

## Rules the journal enforces

- A ticket's plan (shares, entry, stop, target, exit rules, thesis, logbook ID)
  cannot be edited after it is written — only its status can change.
- A fill must reference a ticket and cannot be dated before that ticket.
- Only one open position per ticker.
