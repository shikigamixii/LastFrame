"""Auto-delete logic and the periodic sweep thread.

Webhook flow (_maybe_auto_delete): called immediately after a play event is
stored — deletes only when no grace period is configured.

Sweep flow (_run_sweep / _auto_delete_sweep): runs every 30 minutes,
picks up items whose grace or min-delay window has now elapsed, and items
that never received a webhook (e.g. server owner viewCount fallback).
"""
import logging
import threading
import time
from datetime import datetime, timezone, timedelta

from config_store import load_config
from db import get_db
from plex_api import (
    plex_get, plex_delete, plex_accounts, plex_sections, plex_owner_id,
    parse_plex_guids, ts_to_iso,
)
from assignments import _load_assignment_maps, _resolve_target
from webhook_state import (
    pwe_get_played, pwe_get_played_by_ratingkey, is_item_watched_pwe,
)

logger = logging.getLogger(__name__)


def is_auto_delete_active(db, cfg, library_id, series_id=None, movie_id=None):
    """Return True if auto-delete should fire for this item based on global toggle,
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

def _get_enabled_since(db, cfg, library_id, series_id=None, movie_id=None):
    """Return the UTC datetime from which watch events count for auto-deletion.

    Per-item (series/movie) explicit overrides return None — the user deliberately
    opted this item in, so all past watches are eligible without a timestamp gate.
    Library-level enabled_at is used when auto-delete is on for the whole library.
    """
    if series_id:
        row = db.execute(
            "SELECT enabled FROM auto_delete_overrides WHERE scope='series' AND scope_id=?",
            (str(series_id),)).fetchone()
        if row and row["enabled"]:
            return None  # explicit per-series opt-in: no timestamp gate
    if movie_id:
        row = db.execute(
            "SELECT enabled FROM auto_delete_overrides WHERE scope='movie' AND scope_id=?",
            (str(movie_id),)).fetchone()
        if row and row["enabled"]:
            return None  # explicit per-movie opt-in: no timestamp gate
    enabled_at_map = cfg.get("auto_delete_library_enabled_at") or {}
    ts_str = enabled_at_map.get(str(library_id))
    if ts_str:
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.now(timezone.utc)

def _auto_delete_candidate(db, rating_key, item_type, providers, target_ids, grace_days, enabled_since=None, owner_id=None, item_meta=None, min_delay_minutes=30):
    """Return True if every user in target_ids has watched this item, the watch occurred after opt-in, and grace has elapsed."""
    if not target_ids:
        return False
    played = pwe_get_played(db, list(target_ids), [item_type])
    played_rk = pwe_get_played_by_ratingkey(db, list(target_ids), [item_type])
    rk_watchers = played_rk.get(rating_key, set())
    for aid in target_ids:
        saw_it = (bool(providers) and is_item_watched_pwe(providers, item_type, {aid}, played)) or (aid in rk_watchers)
        # Owner fallback: Plex's viewCount is reliable for the server owner even without a webhook event
        if not saw_it and owner_id and aid == owner_id and item_meta:
            saw_it = bool((item_meta.get("viewCount") or 0) > 0)
        if not saw_it:
            return False
    # Always compute latest_ts — needed for opt-in gate and grace period
    latest_ts = None
    if providers:
        for ptype, pid in providers.items():
            for aid in target_ids:
                row = db.execute(
                    "SELECT updated_at FROM plex_watch_events WHERE plex_account_id=? AND provider_type=? AND provider_id=? AND item_type=? AND event_type='play'",
                    (aid, ptype.lower(), str(pid), item_type)).fetchone()
                if row:
                    ts = row["updated_at"]
                    if latest_ts is None or ts > latest_ts:
                        latest_ts = ts
    if latest_ts is None:
        row = db.execute(
            "SELECT MAX(updated_at) as ts FROM plex_watch_events WHERE rating_key=? AND event_type='play'",
            (rating_key,)).fetchone()
        if row:
            latest_ts = row["ts"]
    # Owner timestamp fallback: use Plex's lastViewedAt when no webhook events exist
    if latest_ts is None and owner_id and item_meta and (item_meta.get("viewCount") or 0) > 0:
        lva = item_meta.get("lastViewedAt")
        if lva:
            latest_ts = ts_to_iso(lva)
    if latest_ts is None:
        return False
    try:
        ts_dt = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
    except Exception:
        return False
    if enabled_since is not None and ts_dt <= enabled_since:
        return False
    if grace_days > 0 and (datetime.now(timezone.utc) - ts_dt) < timedelta(days=grace_days):
        return False
    # Minimum post-scrobble delay prevents deletion when Plex fires scrobble at ~90% completion
    if min_delay_minutes > 0 and (datetime.now(timezone.utc) - ts_dt) < timedelta(minutes=min_delay_minutes):
        return False
    return True

def _maybe_auto_delete(db, cfg, rating_key, item_type, providers, meta):
    """Called from webhook after storing the play event. Deletes if fully watched + grace elapsed."""
    if not cfg.get("auto_delete_enabled"):
        return
    if not rating_key:
        return
    library_id = str(meta.get("librarySectionID") or "")
    series_id = str(meta.get("grandparentRatingKey") or "") if item_type == "episode" else None
    if not library_id:
        try:
            mc = plex_get(f"/library/metadata/{rating_key}")
            item_meta = (mc.get("Metadata") or [{}])[0]
            library_id = str(item_meta.get("librarySectionID") or "")
            if item_type == "episode" and not series_id:
                series_id = str(item_meta.get("grandparentRatingKey") or "")
        except Exception:
            pass
    if not library_id:
        logger.warning(f"Auto-delete: cannot resolve library_id for {rating_key}, skipping")
        return
    movie_id = rating_key if item_type == "movie" else None
    if not is_auto_delete_active(db, cfg, library_id, series_id, movie_id):
        logger.debug(f"Auto-delete: not active for lib={library_id} series={series_id} rk={rating_key}")
        return
    all_account_ids = {a["id"] for a in plex_accounts()}
    if not all_account_ids:
        return
    item_assignments, provider_assignments = _load_assignment_maps(db)
    lookup_id = series_id if (item_type == "episode" and series_id) else rating_key
    target_ids = _resolve_target(providers if item_type == "movie" else {}, lookup_id,
                                 item_assignments, provider_assignments, all_account_ids)
    if not target_ids:
        logger.debug(f"Auto-delete: no target users for {rating_key}, skipping")
        return
    grace_days = int(cfg.get("auto_delete_grace_days") or 0)
    min_delay_minutes = int(cfg.get("auto_delete_min_delay_minutes") or 30)
    enabled_since = _get_enabled_since(db, cfg, library_id, series_id, movie_id)
    owner_id = plex_owner_id()
    if not _auto_delete_candidate(db, rating_key, item_type, providers, target_ids, grace_days, enabled_since,
                                  owner_id=owner_id, item_meta=meta, min_delay_minutes=min_delay_minutes):
        logger.debug(f"Auto-delete: candidate check failed for {rating_key} (not all watched, pre-opt-in, or grace pending)")
        return
    if grace_days > 0 or min_delay_minutes > 0:
        return  # grace or min-delay pending — sweep will handle it
    try:
        title = str(meta.get("title") or rating_key)
        plex_delete(f"/library/metadata/{rating_key}")
        logger.info(f"Auto-deleted {item_type} {rating_key} '{title}' — all assigned users watched")
    except Exception as e:
        logger.warning(f"Auto-delete failed for {rating_key}: {e}")

def _run_sweep(cfg):
    """Periodic sweep: delete anything fully watched where grace period has elapsed."""
    all_account_ids = {a["id"] for a in plex_accounts()}
    if not all_account_ids:
        return
    owner_id = plex_owner_id()
    db = get_db()
    try:
        item_assignments, provider_assignments = _load_assignment_maps(db)
        grace_days = int(cfg.get("auto_delete_grace_days") or 0)
        min_delay_minutes = int(cfg.get("auto_delete_min_delay_minutes") or 30)
        auto_libs = set(str(x) for x in (cfg.get("auto_delete_libraries") or []))
        # Also find libraries needed by force-on series and movie overrides
        override_rows = db.execute(
            "SELECT scope_id FROM auto_delete_overrides WHERE scope='series' AND enabled=1").fetchall()
        force_on_series = {r["scope_id"] for r in override_rows}
        movie_override_rows = db.execute(
            "SELECT scope_id FROM auto_delete_overrides WHERE scope='movie' AND enabled=1").fetchall()
        force_on_movies = {r["scope_id"] for r in movie_override_rows}
        sections = plex_sections()
        section_map = {str(s["key"]): s for s in sections}
        to_scan = set(auto_libs)
        for sid in force_on_series:
            try:
                mc = plex_get(f"/library/metadata/{sid}")
                lib_id = str((mc.get("Metadata") or [{}])[0].get("librarySectionID") or "")
                if lib_id:
                    to_scan.add(lib_id)
            except Exception:
                pass
        for mid in force_on_movies:
            try:
                mc = plex_get(f"/library/metadata/{mid}")
                lib_id = str((mc.get("Metadata") or [{}])[0].get("librarySectionID") or "")
                if lib_id:
                    to_scan.add(lib_id)
            except Exception:
                pass
        for lib_id in to_scan:
            section = section_map.get(lib_id)
            if not section:
                continue
            lib_type = section.get("type", "")
            try:
                if lib_type == "movie":
                    mc = plex_get(f"/library/sections/{lib_id}/all",
                                  {"type": 1, "X-Plex-Container-Size": 10000, "includeGuids": 1})
                    for i in mc.get("Metadata", []):
                        rk = str(i.get("ratingKey", ""))
                        if not rk or not is_auto_delete_active(db, cfg, lib_id, movie_id=rk):
                            continue
                        providers = parse_plex_guids(i.get("Guid", []))
                        target_ids = _resolve_target(providers, rk, item_assignments,
                                                     provider_assignments, all_account_ids)
                        if not target_ids:
                            continue
                        enabled_since = _get_enabled_since(db, cfg, lib_id, movie_id=rk)
                        if not _auto_delete_candidate(db, rk, "movie", providers, target_ids, grace_days, enabled_since,
                                                      owner_id=owner_id, item_meta=i, min_delay_minutes=min_delay_minutes):
                            continue
                        try:
                            plex_delete(f"/library/metadata/{rk}")
                            logger.info(f"Auto-deleted movie {rk} '{i.get('title',rk)}' (sweep)")
                        except Exception as e:
                            logger.warning(f"Sweep auto-delete failed movie {rk}: {e}")
                        time.sleep(0.1)
                elif lib_type == "show":
                    ep_mc = plex_get(f"/library/sections/{lib_id}/all",
                                     {"type": 4, "X-Plex-Container-Size": 50000, "includeGuids": 1})
                    show_providers_cache = {}
                    for ep in ep_mc.get("Metadata", []):
                        rk = str(ep.get("ratingKey", ""))
                        series_id = str(ep.get("grandparentRatingKey", ""))
                        if not rk or not series_id:
                            continue
                        if not is_auto_delete_active(db, cfg, lib_id, series_id):
                            continue
                        if series_id not in show_providers_cache:
                            try:
                                s_mc = plex_get(f"/library/metadata/{series_id}", {"includeGuids": 1})
                                show_providers_cache[series_id] = parse_plex_guids(
                                    (s_mc.get("Metadata") or [{}])[0].get("Guid", []))
                            except Exception:
                                show_providers_cache[series_id] = {}
                        show_providers = show_providers_cache[series_id]
                        target_ids = _resolve_target(show_providers, series_id, item_assignments,
                                                     provider_assignments, all_account_ids)
                        if not target_ids:
                            continue
                        ep_providers = parse_plex_guids(ep.get("Guid", []))
                        enabled_since = _get_enabled_since(db, cfg, lib_id, series_id)
                        if not _auto_delete_candidate(db, rk, "episode", ep_providers, target_ids, grace_days, enabled_since,
                                                      owner_id=owner_id, item_meta=ep, min_delay_minutes=min_delay_minutes):
                            continue
                        try:
                            plex_delete(f"/library/metadata/{rk}")
                            logger.info(f"Auto-deleted episode {rk} '{ep.get('title',rk)}' (sweep)")
                        except Exception as e:
                            logger.warning(f"Sweep auto-delete failed episode {rk}: {e}")
                        time.sleep(0.1)
            except Exception as e:
                logger.warning(f"Sweep error for library {lib_id}: {e}")
    finally:
        db.close()

def _auto_delete_sweep():
    try:
        cfg = load_config()
        if cfg.get("auto_delete_enabled"):
            _run_sweep(cfg)
    except Exception as e:
        logger.warning(f"Auto-delete sweep error: {e}")
    finally:
        t = threading.Timer(1800, _auto_delete_sweep)
        t.daemon = True
        t.start()
