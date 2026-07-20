"""Recently-added media tracking (multi-provider).

The "Recently Added" list is a triage inbox of newly-added movies and TV
series (series-level — never individual episodes). Each title's first_seen date
is recorded once, the first time the discovery sweep observes it, and is never
rewritten — so a quality upgrade or a new weekly episode (both of which bump the
server's own added/created date) can't resurface an already-triaged title.

Rows are keyed by external provider id (Tmdb/Imdb/Tvdb), mirroring
assignments_by_provider, so they survive server item-id changes; items lacking
any provider id fall back to a RatingKey-typed key (the namespaced item id).

Discovery is delegated to each enabled provider engine's recently_added_scan();
this module owns the write-once storage and the read/handle logic.
"""
from datetime import datetime, timezone, timedelta

from config_store import load_config, PROVIDER_PRIORITY
from db import get_db
from media_lookup import get_item_providers
import providers as _providers

DEFAULT_WINDOW_DAYS = 30


def window_days(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    try:
        d = int(cfg.get("recently_added_window_days", DEFAULT_WINDOW_DAYS))
    except (TypeError, ValueError):
        d = DEFAULT_WINDOW_DAYS
    return d if d > 0 else DEFAULT_WINDOW_DAYS


def _provider_key(providers, rating_key):
    """Pick a stable (provider_type, provider_id) for an item, falling back to
    its (namespaced) item id when no external IDs are available."""
    for ptype in PROVIDER_PRIORITY:
        pid = (providers or {}).get(ptype)
        if pid:
            return ptype, str(pid)
    return "RatingKey", str(rating_key)


def discover(cfg=None):
    """Scan every enabled provider and record any title not already tracked.

    INSERT OR IGNORE leaves existing rows untouched (write-once), so first_seen
    stays frozen on later sweeps. Returns the count of newly-recorded titles.
    """
    cfg = cfg if cfg is not None else load_config()
    inserted = 0
    for engine in _providers.enabled_engines(cfg):
        try:
            rows = engine.recently_added_scan(cfg)
        except Exception:
            continue
        if not rows:
            continue
        db = get_db()
        try:
            for r in rows:
                ptype, pid = _provider_key(r.get("providers"), r["rating_key"])
                first_seen = r.get("first_seen") or datetime.now(timezone.utc).isoformat()
                cur = db.execute(
                    "INSERT OR IGNORE INTO recently_added "
                    "(provider_type, provider_id, item_type, library_id, rating_key, title, year, first_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ptype, pid, r["item_type"], r["library_id"], r["rating_key"],
                     r.get("title", ""), r.get("year"), first_seen))
                inserted += cur.rowcount
            db.commit()
        finally:
            db.close()
    return inserted


def get_recent(cfg=None, limit=100):
    """Unhandled titles whose first_seen is inside the rolling window, newest
    first. Shape matches what the home-page cards consume."""
    cfg = cfg if cfg is not None else load_config()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days(cfg))).isoformat()
    db = get_db()
    try:
        rows = db.execute(
            "SELECT item_type, library_id, rating_key, title, year, first_seen "
            "FROM recently_added WHERE handled=0 AND first_seen >= ? "
            "ORDER BY first_seen DESC LIMIT ?",
            (cutoff, int(limit))).fetchall()
    finally:
        db.close()
    return [{"id": r["rating_key"], "type": r["item_type"], "libId": r["library_id"],
             "name": r["title"], "year": r["year"], "addedAt": r["first_seen"]} for r in rows]


def mark_handled(item_id):
    """Flag this title's row(s) as triaged so it drops off the list. Matches by
    item id and by resolved provider ids (either may be how the row was keyed).
    Called whenever an assignment is saved or cleared."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        providers = get_item_providers(item_id)
    except Exception:
        providers = {}
    db = get_db()
    try:
        db.execute("UPDATE recently_added SET handled=1, handled_at=? WHERE rating_key=? AND handled=0",
                   (now, str(item_id)))
        for ptype, pid in (providers or {}).items():
            db.execute("UPDATE recently_added SET handled=1, handled_at=? "
                       "WHERE provider_type=? AND provider_id=? AND handled=0",
                       (now, ptype, str(pid)))
        db.commit()
    finally:
        db.close()
