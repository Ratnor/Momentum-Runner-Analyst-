-- Momentum Runner Scanner journal (data/scanner.db).
-- Applied by scanner.journal.init_db(); every statement is idempotent.
-- Dates are ISO-8601 text: 'YYYY-MM-DD' for sessions, full UTC timestamps otherwise.

PRAGMA foreign_keys = ON;

-- Every ingested filing and headline, before any scoring.
CREATE TABLE IF NOT EXISTS raw_items (
    id              INTEGER PRIMARY KEY,
    source          TEXT    NOT NULL,             -- e.g. 'edgar', 'globenewswire'
    url             TEXT    NOT NULL UNIQUE,      -- de-dup #1
    published_at    TEXT,                          -- source timestamp (acceptance time for EDGAR)
    ingested_at     TEXT    NOT NULL,
    run_date        TEXT    NOT NULL,
    cik             TEXT,
    ticker          TEXT,                          -- primary (first) matched ticker
    form_type       TEXT,                          -- EDGAR form, NULL for wire items
    accession       TEXT,                          -- EDGAR accession number
    match_note      TEXT,                          -- why no ticker matched (NULL if matched)
    headline        TEXT,
    headline_norm   TEXT,                          -- normalized headline for de-dup #2
    text_hash       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_raw_items_ticker_headline ON raw_items (ticker, headline_norm);
CREATE INDEX IF NOT EXISTS ix_raw_items_run_date ON raw_items (run_date);

-- Tickers matched to each raw item (a release can name more than one company).
-- Added in schema v2.
CREATE TABLE IF NOT EXISTS item_tickers (
    raw_item_id  INTEGER NOT NULL REFERENCES raw_items (id) ON DELETE CASCADE,
    ticker       TEXT    NOT NULL,
    exchange     TEXT,                              -- normalized, e.g. 'NASDAQ', 'NYSE AMERICAN'
    method       TEXT    NOT NULL,                  -- 'cik' | 'pattern'
    PRIMARY KEY (raw_item_id, ticker)
);
CREATE INDEX IF NOT EXISTS ix_item_tickers_ticker ON item_tickers (ticker);

-- Every scored item, including rejects, so thresholds can be tuned later.
CREATE TABLE IF NOT EXISTS candidates (
    id                INTEGER PRIMARY KEY,
    run_date          TEXT    NOT NULL,
    raw_item_id       INTEGER REFERENCES raw_items (id),
    ticker            TEXT    NOT NULL,
    catalyst_type     TEXT,                        -- keyword or filing trigger tag
    catalyst_at       TEXT,
    matched_keywords  TEXT,                        -- JSON {phrase: weight}
    headline_score    REAL,
    body_score        REAL,                        -- already multiplied by body_weight
    risk_penalty      REAL,
    risk_flags        TEXT,                        -- JSON {flag: penalty}
    final_score       REAL,
    passed            INTEGER NOT NULL DEFAULT 0 CHECK (passed IN (0, 1)),  -- survived every stage run so far
    drop_stage        TEXT,                        -- stage where it was dropped (NULL if passed)
    drop_reason       TEXT,                        -- human-readable reason (NULL if passed)
    rank              INTEGER
);
CREATE INDEX IF NOT EXISTS ix_candidates_run_ticker ON candidates (run_date, ticker);

-- One row per hard filter per candidate: pass/fail plus the measured value.
CREATE TABLE IF NOT EXISTS candidate_filters (
    candidate_id  INTEGER NOT NULL REFERENCES candidates (id) ON DELETE CASCADE,
    filter_name   TEXT    NOT NULL,                -- matches the config key, e.g. 'rvol_min'
    passed        INTEGER NOT NULL CHECK (passed IN (0, 1)),
    value         REAL,
    threshold     TEXT,
    note          TEXT,
    PRIMARY KEY (candidate_id, filter_name)
);

-- Order tickets. Logbook ID and written exit rules exist before any fill.
CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY,
    logbook_id      TEXT    NOT NULL UNIQUE,       -- MOM-001, MOM-002, ...
    candidate_id    INTEGER REFERENCES candidates (id),
    created_at      TEXT    NOT NULL,
    ticker          TEXT    NOT NULL,
    side            TEXT    NOT NULL DEFAULT 'BUY' CHECK (side IN ('BUY', 'SELL')),
    shares          INTEGER NOT NULL CHECK (shares > 0),
    entry_trigger   REAL    NOT NULL CHECK (entry_trigger > 0),
    entry_limit     REAL    NOT NULL CHECK (entry_limit >= entry_trigger),
    stop_price      REAL    NOT NULL CHECK (stop_price > 0 AND stop_price < entry_trigger),
    target_price    REAL    CHECK (target_price IS NULL OR target_price > entry_trigger),
    risk_dollars    REAL    NOT NULL CHECK (risk_dollars > 0),
    exit_strategy   TEXT    NOT NULL,
    exit_rules      TEXT    NOT NULL,              -- JSON of the exact rules/params in force
    thesis          TEXT    NOT NULL,
    valid_for       TEXT    NOT NULL,              -- session date the order is valid for
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'filled', 'expired', 'cancelled'))
);

-- No retroactive edits: a ticket's plan is frozen once written. Only status may change.
CREATE TRIGGER IF NOT EXISTS tickets_plan_is_frozen
BEFORE UPDATE OF logbook_id, created_at, ticker, side, shares, entry_trigger, entry_limit,
                 stop_price, target_price, risk_dollars, exit_strategy, exit_rules,
                 thesis, valid_for
ON tickets
BEGIN
    SELECT RAISE(ABORT, 'ticket plan is immutable once logged');
END;

-- Open and closed positions, with planned risk vs actual P&L.
CREATE TABLE IF NOT EXISTS positions (
    id                    INTEGER PRIMARY KEY,
    ticket_id             INTEGER NOT NULL UNIQUE REFERENCES tickets (id),
    logbook_id            TEXT    NOT NULL UNIQUE REFERENCES tickets (logbook_id),
    ticker                TEXT    NOT NULL,
    status                TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    opened_on             TEXT    NOT NULL,
    closed_on             TEXT,
    shares_initial        INTEGER NOT NULL CHECK (shares_initial > 0),
    shares_open           INTEGER NOT NULL CHECK (shares_open >= 0),
    entry_price           REAL    NOT NULL,
    stop_price            REAL    NOT NULL,        -- current stop (moves to entry after target)
    target_price          REAL,
    planned_risk_dollars  REAL    NOT NULL,
    highest_close         REAL,
    target_hit            INTEGER NOT NULL DEFAULT 0 CHECK (target_hit IN (0, 1)),
    sessions_held         INTEGER NOT NULL DEFAULT 0,
    realized_pnl          REAL    NOT NULL DEFAULT 0,
    r_multiple            REAL,                    -- realized_pnl / planned_risk_dollars
    exit_rule             TEXT,                    -- rule number/name that closed it
    CHECK (shares_open <= shares_initial),
    CHECK ((status = 'open' AND closed_on IS NULL) OR (status = 'closed' AND closed_on IS NOT NULL))
);
-- One open position per ticker.
CREATE UNIQUE INDEX IF NOT EXISTS ux_positions_one_open_per_ticker
    ON positions (ticker) WHERE status = 'open';

-- Every fill. A fill must reference a ticket, so no fill can exist without a logged plan.
CREATE TABLE IF NOT EXISTS fills (
    id            INTEGER PRIMARY KEY,
    ticket_id     INTEGER NOT NULL REFERENCES tickets (id),
    position_id   INTEGER REFERENCES positions (id),
    filled_on     TEXT    NOT NULL,                -- session date
    side          TEXT    NOT NULL CHECK (side IN ('BUY', 'SELL')),
    shares        INTEGER NOT NULL CHECK (shares > 0),
    ref_price     REAL    NOT NULL,                -- price before slippage
    price         REAL    NOT NULL CHECK (price > 0),
    slippage      REAL    NOT NULL DEFAULT 0,      -- dollars per share
    commission    REAL    NOT NULL DEFAULT 0,
    reason        TEXT    NOT NULL                 -- 'entry', 'stop', 'target', 'trail', ...
);
CREATE INDEX IF NOT EXISTS ix_fills_ticket ON fills (ticket_id);

-- A fill cannot be dated before its ticket was written.
CREATE TRIGGER IF NOT EXISTS fills_not_before_ticket
BEFORE INSERT ON fills
WHEN NEW.filled_on < (SELECT substr(created_at, 1, 10) FROM tickets WHERE id = NEW.ticket_id)
BEGIN
    SELECT RAISE(ABORT, 'fill dated before its ticket was logged');
END;

-- Daily sleeve equity snapshot.
CREATE TABLE IF NOT EXISTS equity (
    session_date     TEXT    PRIMARY KEY,
    equity           REAL    NOT NULL,             -- start + realized + open marked at close
    cash             REAL    NOT NULL,
    realized_pnl     REAL    NOT NULL,
    unrealized_pnl   REAL    NOT NULL,
    open_count       INTEGER NOT NULL,
    kill_switch      INTEGER NOT NULL CHECK (kill_switch IN (0, 1))
);
