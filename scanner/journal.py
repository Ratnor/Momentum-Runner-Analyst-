"""SQLite journal: create the schema and hand out connections.

The journal (data/scanner.db) is the single record of everything the scanner
sees and does. Phase 1 only creates it; later phases add read/write helpers.

Usage:
    python -m scanner.journal                 # create/upgrade DB at settings.data.db_path
    python -m scanner.journal path/to/x.db    # create/upgrade DB at a given path
"""

from __future__ import annotations

import sqlite3
import sys
from importlib import resources
from pathlib import Path

from scanner.config import REPO_ROOT, load_settings

SCHEMA_VERSION = 2

# Steps that upgrade an existing DB *to* the given version. New tables come from
# schema.sql (CREATE ... IF NOT EXISTS); only column changes need a step here.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        "ALTER TABLE raw_items ADD COLUMN accession TEXT",
        "ALTER TABLE raw_items ADD COLUMN match_note TEXT",
    ),
}

EXPECTED_TABLES = (
    "raw_items",
    "item_tickers",
    "candidates",
    "candidate_filters",
    "tickets",
    "positions",
    "fills",
    "equity",
)


def schema_sql() -> str:
    return resources.files("scanner").joinpath("schema.sql").read_text(encoding="utf-8")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the journal with foreign keys enforced and rows addressable by column name."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str | Path) -> sqlite3.Connection:
    """Create the database (and parent folder) from schema.sql. Safe to run repeatedly."""
    p = Path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(p)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"{p} has schema version {current}, newer than this code ({SCHEMA_VERSION})"
        )
    if 0 < current < SCHEMA_VERSION:
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for stmt in MIGRATIONS.get(version, ()):
                conn.execute(stmt)
    conn.executescript(schema_sql())
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return {r[0] for r in rows}


def next_logbook_id(conn: sqlite3.Connection, prefix: str = "MOM") -> str:
    """Next logbook ID (MOM-001, MOM-002, ...) based on tickets already written."""
    row = conn.execute(
        "SELECT MAX(CAST(substr(logbook_id, ?) AS INTEGER)) FROM tickets WHERE logbook_id LIKE ?",
        (len(prefix) + 2, f"{prefix}-%"),
    ).fetchone()
    n = (row[0] or 0) + 1
    return f"{prefix}-{n:03d}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        db_path = Path(args[0])
    else:
        db_path = REPO_ROOT / load_settings().data.db_path
    conn = init_db(db_path)
    tables = table_names(conn)
    conn.close()
    missing = set(EXPECTED_TABLES) - tables
    if missing:
        print(f"ERROR: tables missing after init: {sorted(missing)}", file=sys.stderr)
        return 1
    print(f"Journal ready at {db_path} (schema v{SCHEMA_VERSION}): {', '.join(sorted(tables))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
