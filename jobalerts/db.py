import sqlite3
from datetime import datetime, timezone

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS ads (
    uuid TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    date_modified TEXT,
    title TEXT,
    employer TEXT,
    municipal TEXT,
    url TEXT,
    application_url TEXT,
    description TEXT,
    source TEXT NOT NULL DEFAULT 'nav',
    -- sorting (triage.py)
    triaged_at TEXT,
    relevant INTEGER,
    score INTEGER,
    summary_fr TEXT,  -- summary in the configured LANGUAGE (historical column name)
    norwegian_required INTEGER,
    -- Telegram (telegram.py)
    sent_at TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('keep', 'reject')),
    reason TEXT,
    decided_at TEXT NOT NULL,
    telegram_update_id INTEGER UNIQUE
);
CREATE TABLE IF NOT EXISTS sent_messages (
    message_id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL,
    text TEXT NOT NULL,
    telegram_update_id INTEGER UNIQUE
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)  # the listener and the runs share it
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def get_meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con, key, value):
    con.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    con.commit()


def is_seen(con, uuid: str) -> bool:
    return con.execute("SELECT 1 FROM ads WHERE uuid=?", (uuid,)).fetchone() is not None


def insert_ad(con, ad: dict) -> bool:
    """Insert the ad if unknown. Return True if it is new."""
    cur = con.execute(
        "INSERT OR IGNORE INTO ads(uuid, first_seen, date_modified, title, employer, "
        "municipal, url, application_url, description, source) "
        "VALUES(:uuid, :first_seen, :date_modified, :title, :employer, :municipal, "
        ":url, :application_url, :description, :source)",
        {**ad, "first_seen": now_iso()},
    )
    con.commit()
    return cur.rowcount == 1
