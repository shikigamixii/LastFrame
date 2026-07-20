"""SQLite connection and schema setup."""
import sqlite3
from config_store import DB_PATH

def get_db():
    # Multiple request threads (gunicorn --threads) plus the background sweep
    # timers all write to this DB. Without a busy timeout, concurrent writers
    # raise "database is locked" immediately; WAL lets readers and a writer
    # coexist. Both PRAGMAs are safe to set on every connection.
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    return db

def init_db():
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS assignments (
        item_id TEXT NOT NULL, user_id TEXT NOT NULL,
        PRIMARY KEY (item_id, user_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS assignments_by_provider (
        provider_type TEXT NOT NULL, provider_id TEXT NOT NULL, user_id TEXT NOT NULL,
        PRIMARY KEY (provider_type, provider_id, user_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS watch_events (
        account_id TEXT NOT NULL,
        provider_type TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        item_type TEXT NOT NULL,
        event_type TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        rating_key TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (account_id, provider_type, provider_id, item_type))""")
    try:
        db.execute("ALTER TABLE watch_events ADD COLUMN rating_key TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass
    db.execute("""CREATE TABLE IF NOT EXISTS auto_delete_overrides (
        scope      TEXT NOT NULL,
        scope_id   TEXT NOT NULL,
        enabled    INTEGER NOT NULL,
        enabled_at TEXT,
        PRIMARY KEY (scope, scope_id))""")
    try:
        db.execute("ALTER TABLE auto_delete_overrides ADD COLUMN enabled_at TEXT")
    except Exception:
        pass  # column already exists
    # Recently-added triage list. first_seen is written once (INSERT OR IGNORE)
    # and never updated, so a quality upgrade or new episode — both of which
    # bump Jellyfin's own DateCreated — can't resurface a title already recorded
    # here. Keyed by provider id (like assignments_by_provider) so rows survive
    # item-id changes; items with no provider id fall back to a RatingKey key.
    db.execute("""CREATE TABLE IF NOT EXISTS recently_added (
        provider_type TEXT NOT NULL,
        provider_id   TEXT NOT NULL,
        item_type     TEXT NOT NULL,
        library_id    TEXT NOT NULL DEFAULT '',
        rating_key    TEXT NOT NULL DEFAULT '',
        title         TEXT NOT NULL DEFAULT '',
        year          INTEGER,
        first_seen    TEXT NOT NULL,
        handled       INTEGER NOT NULL DEFAULT 0,
        handled_at    TEXT,
        PRIMARY KEY (provider_type, provider_id))""")
    db.commit(); db.close()
