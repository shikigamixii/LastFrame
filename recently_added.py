"""Recently-added media tracking.

The "Recently Added" list is a triage inbox of newly-added movies and TV
series (series-level — never individual episodes). Each title's first_seen
date is recorded once, the first time the discovery sweep observes it, and is
never rewritten. Because a quality upgrade (better resolution/audio, file
replacement) and a new weekly episode both bump Plex's own addedAt, freezing
first_seen locally is what stops an already-seen title from resurfacing.

Keyed by provider id (Tmdb/Imdb/Tvdb), mirroring assignments_by_provider, so
rows survive Plex rating-key changes. Items lacking any provider id fall back
to a RatingKey-typed key.

A title leaves the list when it ages out of the rolling window or when a user
saves/clears an assignment for it (the assignment endpoints call mark_handled).
"""
from datetime import datetime, timezone, timedelta

from config_store import load_config, PROVIDER_PRIORITY
from db import get_db
from plex_api import (
    plex_get, plex_sections, parse_plex_guids, get_item_providers, ts_to_iso,
)

DEFAULT_WINDOW_DAYS = 30
# Newest-N items pulled per library each sweep. Plex returns them addedAt-desc,
# so the most recent additions are always covered; anything older than the
# rolling window never shows on the list anyway.
_DISCOVERY_LIMIT = 200


def window_days(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    try:
        d = int(cfg.get("recently_added_window_days", DEFAULT_WINDOW_DAYS))
    except (TypeError, ValueError):
        d = DEFAULT_WINDOW_DAYS
    return d if d > 0 else DEFAULT_WINDOW_DAYS


def _provider_key(providers, rating_key):
    """Pick a stable (provider_type, provider_id) for an item, falling back to
    its rating key when no external IDs are available."""
    for ptype in PROVIDER_PRIORITY:
        pid = (providers or {}).get(ptype)
        if pid:
            return ptype, str(pid)
    return "RatingKey", str(rating_key)


def _monitored_sections(cfg, sections):
    """[(library_id, plex_section_type)] for movie/show libraries in scope."""
    show_all = cfg.get("show_all_libraries", True)
    monitored = set(str(x) for x in (cfg.get("monitored_libraries") or []))
    out = []
    for s in sections:
        stype = s.get("type")
        if stype not in ("show", "movie"):
            continue
        lid = str(s.get("key"))
        if show_all or lid in monitored:
            out.append((lid, stype))
    return out


def discover(cfg=None):
    """Scan monitored libraries and record any title not already tracked.

    INSERT OR IGNORE leaves existing rows untouched (write-once), so first_seen
    stays frozen on later sweeps. Returns the count of newly-recorded titles.
    """
    cfg = cfg if cfg is not None else load_config()
    try:
        sections = plex_sections()
    except Exception:
        return 0
    db = get_db()
    inserted = 0
    try:
        for lib_id, stype in _monitored_sections(cfg, sections):
            plex_type = 1 if stype == "movie" else 2
            item_type = "movie" if stype == "movie" else "series"
            try:
                mc = plex_get(f"/library/sections/{lib_id}/all", {
                    "type": plex_type, "sort": "addedAt:desc", "includeGuids": 1,
                    "X-Plex-Container-Size": _DISCOVERY_LIMIT, "X-Plex-Container-Start": 0,
                })
            except Exception:
                continue
            for i in mc.get("Metadata", []):
                rk = str(i.get("ratingKey", ""))
                if not rk:
                    continue
                providers = parse_plex_guids(i.get("Guid", []))
                ptype, pid = _provider_key(providers, rk)
                first_seen = ts_to_iso(i.get("addedAt")) or datetime.now(timezone.utc).isoformat()
                cur = db.execute(
                    "INSERT OR IGNORE INTO recently_added "
                    "(provider_type, provider_id, item_type, library_id, rating_key, title, year, first_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ptype, pid, item_type, lib_id, rk, i.get("title", ""), i.get("year"), first_seen))
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
    rating key and by resolved provider ids (either may be how the row was
    keyed). Called whenever an assignment is saved or cleared."""
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
