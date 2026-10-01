import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import Request

log = logging.getLogger(__name__)

UNCATEGORIZED_NAME = "Uncategorized"
# Always sorts last: new aisles are appended just ahead of it (see repository.next_sort_order).
UNCATEGORIZED_SORT_ORDER = 999_999

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    purchased INTEGER NOT NULL DEFAULT 0,
    quantity REAL,
    unit TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Every item name ever added, with the aisle it was last filed under. Feeds autosuggest on both
-- clients and means the classifier only ever runs once per distinct name.
CREATE TABLE IF NOT EXISTS catalog (
    name TEXT PRIMARY KEY,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL
);

-- Small server-side settings, one JSON document per key (currently just the device sync schedule).
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: each connection is only ever used by one request at a time (get_db
    # opens and closes it within a single request), but FastAPI's sync generator dependencies can
    # resume on a different threadpool worker than the one that opened it, which sqlite3's default
    # same-thread check rejects even though there's no concurrent access.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Off by default in SQLite, per connection. The ON DELETE SET NULL clauses above rely on it.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_items_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial CREATE TABLE. `executescript(SCHEMA)`'s
    CREATE TABLE IF NOT EXISTS only creates the table on a brand-new DB - a deployment that
    already has an `items` table (and rows) needs these added explicitly."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "quantity" not in existing:
        conn.execute("ALTER TABLE items ADD COLUMN quantity REAL")
        log.info("migrated database: added items.quantity", extra={"db.migration": "items.quantity"})
    if "unit" not in existing:
        conn.execute("ALTER TABLE items ADD COLUMN unit TEXT")
        log.info("migrated database: added items.unit", extra={"db.migration": "items.unit"})


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate_items_columns(conn)
        conn.execute(
            "INSERT INTO categories (name, sort_order) VALUES (?, ?) ON CONFLICT(name) DO NOTHING",
            (UNCATEGORIZED_NAME, UNCATEGORIZED_SORT_ORDER),
        )
        conn.commit()
    finally:
        conn.close()


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request, committed if the handler succeeds."""
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
