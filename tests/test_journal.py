from __future__ import annotations

import json
import sqlite3

import pytest

from scanner import journal
from scanner.journal import EXPECTED_TABLES, SCHEMA_VERSION, init_db, next_logbook_id, table_names


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "data" / "scanner.db")
    yield conn
    conn.close()


def _ticket(conn, logbook_id="MOM-001", created_at="2026-11-25T21:45:00Z", **overrides):
    row = dict(
        logbook_id=logbook_id,
        created_at=created_at,
        ticker="ABCD",
        shares=133,
        entry_trigger=2.00,
        entry_limit=2.06,
        stop_price=1.70,
        target_price=4.00,
        risk_dollars=40.0,
        exit_strategy="scale_trail",
        exit_rules=json.dumps({"stop": 1.70, "target_multiple": 2.0}),
        thesis="Phase 3 topline, 6M float, first pullback held midpoint",
        valid_for="2026-11-26",
    )
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(f"INSERT INTO tickets ({cols}) VALUES ({marks})", tuple(row.values()))
    return cur.lastrowid


def test_creates_all_tables_and_parent_dir(tmp_path):
    path = tmp_path / "nested" / "scanner.db"
    conn = init_db(path)
    assert path.exists()
    assert set(EXPECTED_TABLES) <= table_names(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_init_is_idempotent_and_keeps_data(tmp_path):
    path = tmp_path / "scanner.db"
    conn = init_db(path)
    _ticket(conn)
    conn.commit()
    conn.close()
    conn = init_db(path)
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1
    conn.close()


def test_refuses_newer_schema(tmp_path):
    path = tmp_path / "scanner.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        init_db(path)


def test_foreign_keys_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO fills (ticket_id, filled_on, side, shares, ref_price, price, reason) "
            "VALUES (999, '2026-11-26', 'BUY', 10, 2.0, 2.02, 'entry')"
        )


def test_raw_items_dedup_by_url(db):
    sql = (
        "INSERT INTO raw_items (source, url, ingested_at, run_date, text_hash) "
        "VALUES ('edgar', 'https://x/1', '2026-11-25T21:45:00Z', '2026-11-25', 'h')"
    )
    db.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql)


def test_ticket_plan_is_frozen_but_status_can_change(db):
    tid = _ticket(db)
    db.execute("UPDATE tickets SET status = 'filled' WHERE id = ?", (tid,))
    for col, val in [("stop_price", 1.50), ("target_price", 5.0), ("exit_rules", "{}"), ("shares", 500)]:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(f"UPDATE tickets SET {col} = ? WHERE id = ?", (val, tid))


def test_ticket_requires_thesis_and_exit_rules(db):
    with pytest.raises(sqlite3.IntegrityError):
        _ticket(db, thesis=None)
    with pytest.raises(sqlite3.IntegrityError):
        _ticket(db, logbook_id="MOM-002", exit_rules=None)


def test_ticket_stop_must_be_below_entry(db):
    with pytest.raises(sqlite3.IntegrityError):
        _ticket(db, stop_price=2.10)


def test_fill_cannot_predate_ticket(db):
    tid = _ticket(db, created_at="2026-11-25T21:45:00Z")
    with pytest.raises(sqlite3.IntegrityError, match="before its ticket"):
        db.execute(
            "INSERT INTO fills (ticket_id, filled_on, side, shares, ref_price, price, reason) "
            "VALUES (?, '2026-11-24', 'BUY', 133, 2.0, 2.02, 'entry')",
            (tid,),
        )
    db.execute(
        "INSERT INTO fills (ticket_id, filled_on, side, shares, ref_price, price, reason) "
        "VALUES (?, '2026-11-26', 'BUY', 133, 2.0, 2.02, 'entry')",
        (tid,),
    )


def test_one_open_position_per_ticker(db):
    t1 = _ticket(db, "MOM-001")
    t2 = _ticket(db, "MOM-002")
    sql = (
        "INSERT INTO positions (ticket_id, logbook_id, ticker, opened_on, shares_initial, "
        "shares_open, entry_price, stop_price, planned_risk_dollars) "
        "VALUES (?, ?, 'ABCD', '2026-11-26', 133, 133, 2.02, 1.70, 40.0)"
    )
    db.execute(sql, (t1, "MOM-001"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, (t2, "MOM-002"))
    # once the first is closed, a new position on the same ticker is allowed
    db.execute("UPDATE positions SET status = 'closed', closed_on = '2026-12-01' WHERE ticket_id = ?", (t1,))
    db.execute(sql, (t2, "MOM-002"))


def test_next_logbook_id(db):
    assert next_logbook_id(db) == "MOM-001"
    _ticket(db, "MOM-001")
    _ticket(db, "MOM-009")
    assert next_logbook_id(db) == "MOM-010"


def test_cli_creates_db(tmp_path, capsys):
    path = tmp_path / "cli.db"
    assert journal.main([str(path)]) == 0
    assert path.exists()
    assert "Journal ready" in capsys.readouterr().out
