"""Provider-agnostic auto-delete gating.

These helpers operate purely on the local DB (watch_events, auto_delete_overrides)
and the config dict. All ids passed in — library_id, series_id, movie_id,
rating_key, account/target ids — are the namespaced ids used everywhere the
data crosses the DB boundary (see idutil). The provider-specific parts (fetching
metadata, resolving the owner's watched state, iterating libraries, deleting)
live in each engine and feed their results into candidate_ok().
"""
from datetime import datetime, timezone, timedelta

from webhook_state import (
    pwe_get_played, pwe_get_played_by_ratingkey, is_item_watched_pwe,
)


def is_auto_delete_active(db, cfg, library_id, series_id=None, movie_id=None):
    """True if auto-delete should fire for this item based on the global toggle,
    per-library opt-in, and per-series/per-movie overrides."""
    if not cfg.get("auto_delete_enabled"):
        return False
    if series_id:
        row = db.execute(
            "SELECT enabled FROM auto_delete_overrides WHERE scope='series' AND scope_id=?",
            (str(series_id),)).fetchone()
        if row is not None:
            return bool(row["enabled"])
    if movie_id:
        row = db.execute(
            "SELECT enabled FROM auto_delete_overrides WHERE scope='movie' AND scope_id=?",
            (str(movie_id),)).fetchone()
        if row is not None:
            return bool(row["enabled"])
    return str(library_id) in [str(x) for x in (cfg.get("auto_delete_libraries") or [])]


def get_enabled_since(db, cfg, library_id, series_id=None, movie_id=None):
    """UTC datetime from which watch events count for auto-deletion.

    Explicit per-item (series/movie) opt-ins return None — the user deliberately
    opted this item in, so all past watches are eligible with no timestamp gate.
    Library-level enabled_at gates library opt-ins.
    """
    if series_id:
        row = db.execute(
            "SELECT enabled FROM auto_delete_overrides WHERE scope='series' AND scope_id=?",
            (str(series_id),)).fetchone()
        if row and row["enabled"]:
            return None
    if movie_id:
        row = db.execute(
            "SELECT enabled FROM auto_delete_overrides WHERE scope='movie' AND scope_id=?",
            (str(movie_id),)).fetchone()
        if row and row["enabled"]:
            return None
    enabled_at_map = cfg.get("auto_delete_library_enabled_at") or {}
    ts_str = enabled_at_map.get(str(library_id))
    if ts_str:
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def latest_watch_ts(db, rating_key, item_type, providers, target_ids,
                    owner_saw=False, owner_ts=None):
    """Most recent play timestamp (ISO str) for this item across target users.

    Prefers provider-keyed events, falls back to rating_key events, then to the
    server-owner's native watched timestamp (owner_ts) when supplied.
    """
    latest = None
    if providers:
        for ptype, pid in providers.items():
            for aid in target_ids:
                row = db.execute(
                    "SELECT updated_at FROM watch_events WHERE account_id=? AND provider_type=? "
                    "AND provider_id=? AND item_type=? AND event_type='play'",
                    (aid, ptype.lower(), str(pid), item_type)).fetchone()
                if row and (latest is None or row["updated_at"] > latest):
                    latest = row["updated_at"]
    if latest is None:
        row = db.execute(
            "SELECT MAX(updated_at) as ts FROM watch_events WHERE rating_key=? AND event_type='play'",
            (rating_key,)).fetchone()
        if row and row["ts"]:
            latest = row["ts"]
    if latest is None and owner_saw and owner_ts:
        latest = owner_ts
    return latest


def candidate_ok(db, rating_key, item_type, providers, target_ids, grace_days,
                 enabled_since=None, owner_id=None, owner_saw=False, owner_ts=None,
                 min_delay_minutes=30):
    """True when every target user has watched the item (webhook events, or the
    owner's native watched state), the last watch is after opt-in, and both the
    grace period and the minimum post-completion delay have elapsed."""
    if not target_ids:
        return False
    played = pwe_get_played(db, list(target_ids), [item_type])
    played_rk = pwe_get_played_by_ratingkey(db, list(target_ids), [item_type])
    rk_watchers = played_rk.get(rating_key, set())
    for aid in target_ids:
        saw_it = (bool(providers) and is_item_watched_pwe(providers, item_type, {aid}, played)) \
            or (aid in rk_watchers)
        if not saw_it and owner_id and aid == owner_id and owner_saw:
            saw_it = True
        if not saw_it:
            return False
    latest = latest_watch_ts(db, rating_key, item_type, providers, target_ids, owner_saw, owner_ts)
    if latest is None:
        return False
    try:
        ts_dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    except Exception:
        return False
    if enabled_since is not None and ts_dt <= enabled_since:
        return False
    now = datetime.now(timezone.utc)
    if grace_days > 0 and (now - ts_dt) < timedelta(days=grace_days):
        return False
    if min_delay_minutes > 0 and (now - ts_dt) < timedelta(minutes=min_delay_minutes):
        return False
    return True
