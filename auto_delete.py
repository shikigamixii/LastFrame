"""Auto-delete logic and the periodic sweep thread.

Webhook flow (_maybe_auto_delete): called immediately after a play event is
stored — deletes only when no grace period is configured.

Sweep flow (_run_sweep / _auto_delete_sweep): runs every 30 minutes,
picks up items whose grace or min-delay window has now elapsed, and items
that never received a webhook (e.g. admin UserData.Played fallback).
"""
import logging
import threading
import time
from datetime import datetime, timezone, timedelta

from config_store import load_config
from db import get_db
from jellyfin_api import (
    jellyfin_get, jellyfin_get_item, jellyfin_delete, jellyfin_all_users,
    jellyfin_libraries, jellyfin_items, jellyfin_admin_id,
    parse_jellyfin_providers,
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
        # Admin fallback: Jellyfin UserData.Played is available when item was fetched with userId
        if not saw_it and owner_id and aid == owner_id and item_meta:
            saw_it = bool(item_meta.get("UserData", {}).get("Played", False))
        if not saw_it:
            return False
    # Always compute latest_ts — needed for opt-in gate and grace period
    latest_ts = None
    if providers:
        for ptype, pid in providers.items():
            for aid in target_ids:
                row = db.execute(
                    "SELECT updated_at FROM watch_events WHERE account_id=? AND provider_type=? AND provider_id=? AND item_type=? AND event_type='play'",
                    (aid, ptype.lower(), str(pid), item_type)).fetchone()
                if row:
                    ts = row["updated_at"]
                    if latest_ts is None or ts > latest_ts:
                        latest_ts = ts
    if latest_ts is None:
        row = db.execute(
            "SELECT MAX(updated_at) as ts FROM watch_events WHERE rating_key=? AND event_type='play'",
            (rating_key,)).fetchone()
        if row:
            latest_ts = row["ts"]
    # Admin timestamp fallback: use Jellyfin UserData.LastPlayedDate when no webhook events exist
    if latest_ts is None and owner_id and item_meta \
            and item_meta.get("UserData", {}).get("Played", False):
        latest_ts = item_meta.get("UserData", {}).get("LastPlayedDate")
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
    # Minimum post-completion delay before deletion is allowed
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
    series_id = str(meta.get("SeriesId") or meta.get("grandparentRatingKey") or "") if item_type == "episode" else None
    if not library_id:
        try:
            data = jellyfin_get_item(rating_key, {"fields": "ParentId"})
            library_id = str(data.get("ParentId") or "")
            if item_type == "episode" and not series_id:
                series_id = str(data.get("SeriesId") or "")
        except Exception:
            pass
    if not library_id:
        logger.warning(f"Auto-delete: cannot resolve library_id for {rating_key}, skipping")
        return
    movie_id = rating_key if item_type == "movie" else None
    if not is_auto_delete_active(db, cfg, library_id, series_id, movie_id):
        logger.debug(f"Auto-delete: not active for lib={library_id} series={series_id} rk={rating_key}")
        return
    all_account_ids = {a["id"] for a in jellyfin_all_users()}
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
    owner_id = jellyfin_admin_id()
    if not _auto_delete_candidate(db, rating_key, item_type, providers, target_ids, grace_days, enabled_since,
                                  owner_id=owner_id, item_meta=meta, min_delay_minutes=min_delay_minutes):
        logger.debug(f"Auto-delete: candidate check failed for {rating_key} (not all watched, pre-opt-in, or grace pending)")
        return
    if grace_days > 0 or min_delay_minutes > 0:
        return  # grace or min-delay pending — sweep will handle it
    try:
        title = str(meta.get("Name") or meta.get("title") or rating_key)
        jellyfin_delete(rating_key)
        logger.info(f"Auto-deleted {item_type} {rating_key} '{title}' — all assigned users watched")
    except Exception as e:
        logger.warning(f"Auto-delete failed for {rating_key}: {e}")

def _run_sweep(cfg):
    """Periodic sweep: delete anything fully watched where grace period has elapsed."""
    all_account_ids = {a["id"] for a in jellyfin_all_users()}
    if not all_account_ids:
        return
    owner_id = jellyfin_admin_id()
    db = get_db()
    try:
        item_assignments, provider_assignments = _load_assignment_maps(db)
        grace_days = int(cfg.get("auto_delete_grace_days") or 0)
        min_delay_minutes = int(cfg.get("auto_delete_min_delay_minutes") or 30)
        auto_libs = set(str(x) for x in (cfg.get("auto_delete_libraries") or []))
        override_rows = db.execute(
            "SELECT scope_id FROM auto_delete_overrides WHERE scope='series' AND enabled=1").fetchall()
        force_on_series = {r["scope_id"] for r in override_rows}
        movie_override_rows = db.execute(
            "SELECT scope_id FROM auto_delete_overrides WHERE scope='movie' AND enabled=1").fetchall()
        force_on_movies = {r["scope_id"] for r in movie_override_rows}
        sections = jellyfin_libraries()
        section_map = {str(s["Id"]): s for s in sections}
        logger.info(f"Sweep start: auto_libs={len(auto_libs)} force_series={len(force_on_series)} "
                    f"force_movies={len(force_on_movies)} grace_days={grace_days} min_delay={min_delay_minutes}")
        deleted_count = 0

        # Library-opt-in path: iterate libraries in auto_libs.
        for lib_id in auto_libs:
            section = section_map.get(lib_id)
            if not section:
                logger.info(f"Sweep: skip lib {lib_id} (not in MediaFolders)")
                continue
            lib_type = (section.get("CollectionType") or "").lower()
            try:
                if lib_type == "movies":
                    items = jellyfin_items({
                        "ParentId": lib_id, "Recursive": "true",
                        "IncludeItemTypes": "Movie", "fields": "ProviderIds,UserData",
                        "userId": owner_id, "Limit": 10000
                    })
                    for i in items:
                        rk = str(i.get("Id", ""))
                        if not rk or not is_auto_delete_active(db, cfg, lib_id, movie_id=rk):
                            continue
                        providers = parse_jellyfin_providers(i.get("ProviderIds") or {})
                        target_ids = _resolve_target(providers, rk, item_assignments,
                                                     provider_assignments, all_account_ids)
                        if not target_ids:
                            continue
                        enabled_since = _get_enabled_since(db, cfg, lib_id, movie_id=rk)
                        if not _auto_delete_candidate(db, rk, "movie", providers, target_ids, grace_days, enabled_since,
                                                      owner_id=owner_id, item_meta=i, min_delay_minutes=min_delay_minutes):
                            continue
                        try:
                            jellyfin_delete(rk)
                            logger.info(f"Auto-deleted movie {rk} '{i.get('Name',rk)}' (sweep lib)")
                            deleted_count += 1
                        except Exception as e:
                            logger.warning(f"Sweep auto-delete failed movie {rk}: {e}")
                        time.sleep(0.1)
                elif lib_type == "tvshows":
                    episodes = jellyfin_items({
                        "ParentId": lib_id, "Recursive": "true",
                        "IncludeItemTypes": "Episode", "fields": "ProviderIds,UserData,SeriesId",
                        "userId": owner_id, "Limit": 50000
                    })
                    show_providers_cache = {}
                    for ep in episodes:
                        rk = str(ep.get("Id", ""))
                        series_id = str(ep.get("SeriesId", ""))
                        if not rk or not series_id:
                            continue
                        if not is_auto_delete_active(db, cfg, lib_id, series_id):
                            continue
                        if series_id not in show_providers_cache:
                            try:
                                s_data = jellyfin_get_item(series_id, {"fields": "ProviderIds"})
                                show_providers_cache[series_id] = parse_jellyfin_providers(
                                    s_data.get("ProviderIds") or {})
                            except Exception:
                                show_providers_cache[series_id] = {}
                        show_providers = show_providers_cache[series_id]
                        target_ids = _resolve_target(show_providers, series_id, item_assignments,
                                                     provider_assignments, all_account_ids)
                        if not target_ids:
                            continue
                        ep_providers = parse_jellyfin_providers(ep.get("ProviderIds") or {})
                        enabled_since = _get_enabled_since(db, cfg, lib_id, series_id)
                        if not _auto_delete_candidate(db, rk, "episode", ep_providers, target_ids, grace_days, enabled_since,
                                                      owner_id=owner_id, item_meta=ep, min_delay_minutes=min_delay_minutes):
                            continue
                        try:
                            jellyfin_delete(rk)
                            logger.info(f"Auto-deleted episode {rk} '{ep.get('Name',rk)}' (sweep lib)")
                            deleted_count += 1
                        except Exception as e:
                            logger.warning(f"Sweep auto-delete failed episode {rk}: {e}")
                        time.sleep(0.1)
            except Exception as e:
                logger.warning(f"Sweep error for library {lib_id}: {e}")

        # Force-on series path: iterate each opted-in series directly. Does NOT
        # depend on resolving series.ParentId to a media folder, which doesn't
        # always match /Library/MediaFolders.
        for series_id in force_on_series:
            try:
                try:
                    s_data = jellyfin_get_item(series_id, {"fields": "ProviderIds"})
                    show_providers = parse_jellyfin_providers(s_data.get("ProviderIds") or {})
                except Exception:
                    show_providers = {}
                target_ids = _resolve_target(show_providers, series_id, item_assignments,
                                             provider_assignments, all_account_ids)
                if not target_ids:
                    logger.info(f"Sweep: force-on series {series_id} has no target users")
                    continue
                episodes = jellyfin_items({
                    "ParentId": series_id, "Recursive": "true",
                    "IncludeItemTypes": "Episode", "fields": "ProviderIds,UserData",
                    "userId": owner_id, "Limit": 50000
                })
                logger.info(f"Sweep: force-on series {series_id} → {len(episodes)} episodes, "
                            f"targets={sorted(target_ids)}")
                for ep in episodes:
                    rk = str(ep.get("Id", ""))
                    if not rk:
                        continue
                    ep_providers = parse_jellyfin_providers(ep.get("ProviderIds") or {})
                    if not _auto_delete_candidate(db, rk, "episode", ep_providers, target_ids, grace_days,
                                                  enabled_since=None,
                                                  owner_id=owner_id, item_meta=ep,
                                                  min_delay_minutes=min_delay_minutes):
                        continue
                    try:
                        jellyfin_delete(rk)
                        logger.info(f"Auto-deleted episode {rk} '{ep.get('Name',rk)}' (sweep force-on series {series_id})")
                        deleted_count += 1
                    except Exception as e:
                        logger.warning(f"Sweep auto-delete failed episode {rk}: {e}")
                    time.sleep(0.1)
            except Exception as e:
                logger.warning(f"Sweep error for force-on series {series_id}: {e}")

        # Force-on movies path: each opted-in movie checked directly.
        for movie_id in force_on_movies:
            try:
                m = jellyfin_get_item(movie_id, {"fields": "ProviderIds,UserData"})
                providers = parse_jellyfin_providers(m.get("ProviderIds") or {})
                target_ids = _resolve_target(providers, movie_id, item_assignments,
                                             provider_assignments, all_account_ids)
                if not target_ids:
                    logger.info(f"Sweep: force-on movie {movie_id} has no target users")
                    continue
                if not _auto_delete_candidate(db, movie_id, "movie", providers, target_ids, grace_days,
                                              enabled_since=None,
                                              owner_id=owner_id, item_meta=m,
                                              min_delay_minutes=min_delay_minutes):
                    continue
                try:
                    jellyfin_delete(movie_id)
                    logger.info(f"Auto-deleted movie {movie_id} '{m.get('Name',movie_id)}' (sweep force-on movie)")
                    deleted_count += 1
                except Exception as e:
                    logger.warning(f"Sweep auto-delete failed movie {movie_id}: {e}")
            except Exception as e:
                logger.warning(f"Sweep error for force-on movie {movie_id}: {e}")

        logger.info(f"Sweep complete: {deleted_count} deletions")
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
