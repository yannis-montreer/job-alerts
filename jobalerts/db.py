import sqlite3
from datetime import datetime, timezone

from .config import DB_PATH

# ads: one row per job ad, shared by every profile (what the ad says).
# matches: one row per (profile, ad): the sorting result and when it was sent, for that profile.
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
    -- before profiles (kept for old databases, no longer used)
    triaged_at TEXT, relevant INTEGER, score INTEGER, summary_fr TEXT, norwegian_required INTEGER, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS matches (
    profile_id TEXT NOT NULL,
    uuid TEXT NOT NULL,
    triaged_at TEXT,
    relevant INTEGER,
    score INTEGER,
    summary TEXT,  -- in the profile's language
    norwegian_required INTEGER,
    sent_at TEXT,
    PRIMARY KEY (profile_id, uuid)
);
CREATE TABLE IF NOT EXISTS messages (
    bot_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    profile_id TEXT NOT NULL,
    uuid TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (bot_id, message_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

DECISIONS = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL DEFAULT 'p1',
    uuid TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('keep', 'reject')),
    reason TEXT,
    decided_at TEXT NOT NULL,
    bot_id TEXT NOT NULL DEFAULT 'b1',
    telegram_update_id INTEGER,
    UNIQUE (bot_id, telegram_update_id)
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL DEFAULT 'p1',
    uuid TEXT NOT NULL,
    text TEXT NOT NULL,
    bot_id TEXT NOT NULL DEFAULT 'b1',
    telegram_update_id INTEGER,
    UNIQUE (bot_id, telegram_update_id)
);
CREATE TABLE IF NOT EXISTS kits (
    profile_id TEXT NOT NULL DEFAULT 'p1',
    uuid TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    content TEXT,
    sent_at TEXT,
    docs_attempts INTEGER NOT NULL DEFAULT 0,
    docs_at TEXT,
    docs_error TEXT,
    PRIMARY KEY (profile_id, uuid)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _columns(con, table) -> set:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _migrate(con):
    """Databases from before profiles: everything becomes profile p1 / bot b1. Runs once."""
    old = [t for t in ("decisions", "notes", "kits") if _columns(con, t) and "profile_id" not in _columns(con, t)]
    first = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone() is None
    if not old and not first:
        return
    con.execute("BEGIN")
    try:
        for t in old:
            con.execute(f"ALTER TABLE {t} RENAME TO {t}_v1")
        for stmt in DECISIONS.split(";"):
            if stmt.strip():
                con.execute(stmt)
        if "decisions" in old:
            con.execute("INSERT INTO decisions(id, profile_id, uuid, decision, reason, decided_at, bot_id, telegram_update_id) "
                        "SELECT id, 'p1', uuid, decision, reason, decided_at, 'b1', telegram_update_id FROM decisions_v1")
        if "notes" in old:
            con.execute("INSERT INTO notes(id, profile_id, uuid, text, bot_id, telegram_update_id) "
                        "SELECT id, 'p1', uuid, text, 'b1', telegram_update_id FROM notes_v1")
        if "kits" in old:
            con.execute("INSERT INTO kits(profile_id, uuid, attempts, error, content, sent_at) "
                        "SELECT 'p1', uuid, attempts, error, content, sent_at FROM kits_v1")
        if first:
            con.execute("INSERT OR IGNORE INTO matches(profile_id, uuid, triaged_at, relevant, score, summary, "
                        "norwegian_required, sent_at) SELECT 'p1', uuid, triaged_at, relevant, score, summary_fr, "
                        "norwegian_required, sent_at FROM ads")
            if _columns(con, "sent_messages"):
                con.execute("INSERT OR IGNORE INTO messages(bot_id, message_id, profile_id, uuid, sent_at) "
                            "SELECT 'b1', message_id, 'p1', uuid, sent_at FROM sent_messages")
            for key in ("tg_offset", "tg_listener_seen"):
                con.execute("INSERT OR IGNORE INTO meta(key, value) SELECT ?, value FROM meta WHERE key=?",
                            (f"{key}:b1", key))
            con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema', '2')")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)  # the listener and the runs share it
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate_connection(con)
    con.executescript(DECISIONS)
    cols = _columns(con, "kits")
    for col, decl in (("docs_attempts", "INTEGER NOT NULL DEFAULT 0"), ("docs_at", "TEXT"), ("docs_error", "TEXT")):
        if col not in cols:
            con.execute(f"ALTER TABLE kits ADD COLUMN {col} {decl}")
    return con


def _migrate_connection(con):
    con.isolation_level = None  # explicit BEGIN / COMMIT in _migrate
    try:
        _migrate(con)
    finally:
        con.isolation_level = ""


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


def add_match(con, profile_id: str, uuid: str) -> bool:
    """The ad is a candidate for this profile (to be sorted for it). True if new."""
    cur = con.execute("INSERT OR IGNORE INTO matches(profile_id, uuid) VALUES(?, ?)", (profile_id, uuid))
    con.commit()
    return cur.rowcount == 1
