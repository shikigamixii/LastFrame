"""SQLite connection and schema setup."""
import sqlite3
from config_store import DB_PATH

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS assignments (
        item_id TEXT NOT NULL, user_id TEXT NOT NULL,
        PRIMARY KEY (item_id, user_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS assignments_by_provider (
        provider_type TEXT NOT NULL, provider_id TEXT NOT NULL, user_id TEXT NOT NULL,
        PRIMARY KEY (provider_type, provider_id, user_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS plex_watch_events (
        plex_account_id TEXT NOT NULL,
        provider_type TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        item_type TEXT NOT NULL,
        event_type TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        rating_key TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (plex_account_id, provider_type, provider_id, item_type))""")
    try:
        db.execute("ALTER TABLE plex_watch_events ADD COLUMN rating_key TEXT NOT NULL DEFAULT ''")
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
    db.commit(); db.close()
