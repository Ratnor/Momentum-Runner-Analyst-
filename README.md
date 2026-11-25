# Momentum Runner Scanner

Finds low-float US micro-cap momentum runners (biotech and pre-revenue tech)
from fresh catalysts, sizes positions automatically, and paper-trades them so
the strategy can be judged on 6–8 weeks of forward results before any real money.

## Build status

| Phase | Status |
| --- | --- |
| 1. Skeleton: config loading, SQLite schema, `Broker` interface, CI | ✅ done |
| 2. Ingest + scoring | not started |
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
scanner/sources/          data adapters (phase 2-3)
scanner/strategies/       entry/exit strategies (phase 4)
backtest/                 exit-rule comparison (phase 6)
tests/                    pytest, no network calls
```

## Run locally

```bash
pip install -e ".[dev]"
pytest                    # run the tests
python -m scanner.journal # create/upgrade data/scanner.db
```

## Rules the journal enforces

- A ticket's plan (shares, entry, stop, target, exit rules, thesis, logbook ID)
  cannot be edited after it is written — only its status can change.
- A fill must reference a ticket and cannot be dated before that ticket.
- Only one open position per ticker.
