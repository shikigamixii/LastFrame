"""Jellyfin provider engine.

Wraps jellyfin_api into the uniform engine interface app.py consumes. Every id
returned to the app/frontend is namespaced ('jf_<id>'); ids are stripped back to
raw only when talking to Jellyfin's HTTP API. Watch state, assignments and
auto-delete all operate on the namespaced ids via the shared modules.
"""
import logging
import time
from datetime import datetime, timezone, timedelta

import idutil
import media_lookup
from flask import Response
import requests as http_requests

from config_store import load_config, PAGE_SIZE
from db import get_db
from jellyfin_api import (
    get_jellyfin_url, get_jellyfin_api_key, get_webhook_secret, _jf_headers,
    jellyfin_get, jellyfin_get_item, jellyfin_delete, jellyfin_users,
    jellyfin_all_users, jellyfin_admin_id, jellyfin_libraries, jellyfin_items,
    ts_to_iso, parse_jellyfin_providers, get_item_providers as _raw_item_providers,
)
from assignments import get_assigned_ids, _load_assignment_maps, _resolve_target
from webhook_state import (
    pwe_has_data, pwe_get_played, pwe_get_played_with_ts, pwe_get_played_by_ratingkey,
    is_item_watched_pwe, get_last_played_pwe,
)
import auto_delete_core as adc

logger = logging.getLogger(__name__)

KEY = "jellyfin"
LABEL = "Jellyfin"
WEBHOOK_PATH = "/api/webhook/jellyfin"

# 32-char hex, dashed GUID, or legacy numeric.
import re
_RAW_ID_RE = re.compile(
    r'^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|\d{1,20})$')


def _ns(raw):
    return idutil.make_id(KEY, raw)


def valid_raw(raw):
    return bool(raw and _RAW_ID_RE.match(str(raw)))


# ── identity / config ─────────────────────────────────────────────────
def is_configured():
    return bool(get_jellyfin_api_key())


def is_enabled(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    return bool(cfg.get("jellyfin_enabled")) and is_configured()


def webhook_secret():
    return get_webhook_secret()


def get_item_providers(raw):
    return _raw_item_providers(raw)


# ── accounts ──────────────────────────────────────────────────────────
def _accounts_full():
    """Visible accounts with both namespaced id and raw id (for API calls)."""
    return [{"id": _ns(u["id"]), "raw": u["id"], "name": u["name"]} for u in jellyfin_users()]


def accounts():
    return [{"id": a["id"], "name": a["name"]} for a in _accounts_full()]


def all_accounts():
    cfg = load_config()
    hidden = set(cfg.get("hidden_accounts", []))
    out = []
    for u in jellyfin_all_users():
        nsid = _ns(u["id"])
        out.append({"id": nsid, "name": u["name"] or f"(unnamed #{u['id']})",
                    "hidden": nsid in hidden})
    return out


def owner_id():
    aid = jellyfin_admin_id()
    return _ns(aid) if aid else None


# ── libraries / browse ────────────────────────────────────────────────
def libraries(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    out = []
    for s in jellyfin_libraries():
        ctype = (s.get("CollectionType") or "").lower()
        if ctype not in ("tvshows", "movies"):
            continue
        nsid = _ns(str(s["Id"]))
        out.append({"id": nsid, "name": s.get("Name", ""), "type": ctype,
                    "monitored": cfg.get("show_all_libraries", True) or nsid in cfg.get("monitored_libraries", [])})
    return out


def titles_by_ids(kind, raw_ids):
    items = []
    for rid in raw_ids:
        if not valid_raw(rid):
            continue
        try:
            m = jellyfin_get_item(rid)
            items.append({"id": _ns(m["Id"]), "name": m.get("Name", ""), "year": m.get("ProductionYear")})
        except Exception:
            continue
    return items


def list_titles(kind, parent_raw, page, search, genre):
    jf_type = "Movie" if kind == "movies" else "Series"
    params = {"ParentId": parent_raw, "IncludeItemTypes": jf_type, "Recursive": "true",
              "SortBy": "SortName", "SortOrder": "Ascending",
              "Limit": 100 if search else PAGE_SIZE,
              "StartIndex": 0 if search else (page - 1) * PAGE_SIZE}
    if search:
        params["SearchTerm"] = search
    if genre:
        params["Genres"] = genre
    data = jellyfin_get("/Items", params)
    items = data.get("Items", [])
    total = data.get("TotalRecordCount", len(items))
    return {"items": [{"id": _ns(i["Id"]), "name": i.get("Name", ""), "year": i.get("ProductionYear")} for i in items],
            "totalCount": total}


def genres(parent_raw):
    mc = jellyfin_get("/Genres", {"ParentId": parent_raw, "Limit": 1000})
    return [g.get("Name", "") for g in mc.get("Items", []) if g.get("Name")]


def search(cfg):
    show_all = cfg.get("show_all_libraries", True)
    monitored = set(cfg.get("monitored_libraries", []))
    q = cfg.get("_q", "")
    results = []
    for s in jellyfin_libraries():
        ctype = (s.get("CollectionType") or "").lower()
        if ctype not in ("tvshows", "movies"):
            continue
        nsid = _ns(str(s["Id"]))
        if not show_all and nsid not in monitored:
            continue
        jf_type = "Movie" if ctype == "movies" else "Series"
        kind = "movie" if ctype == "movies" else "series"
        try:
            data = jellyfin_get("/Items", {
                "ParentId": str(s["Id"]), "IncludeItemTypes": jf_type, "Recursive": "true",
                "SearchTerm": q, "Limit": 25, "SortBy": "SortName", "SortOrder": "Ascending",
            })
        except Exception:
            continue
        for i in data.get("Items", []):
            results.append({"id": _ns(i["Id"]), "name": i.get("Name", ""),
                            "year": i.get("ProductionYear"), "type": kind, "libId": nsid})
    return results


# ── item info ─────────────────────────────────────────────────────────
def item_info(raw):
    i = jellyfin_get_item(raw, {"fields": "ProviderIds"})
    if not i:
        return None
    jf_type = i.get("Type", "")
    type_map = {"Series": ("tvshows", "Series"), "Movie": ("movies", "Movie"),
                "Season": ("tvshows", "Season"), "Episode": ("tvshows", "Episode")}
    ctype, itype = type_map.get(jf_type, ("", jf_type))
    return {
        "id": _ns(str(i.get("Id", raw))),
        "name": i.get("Name", ""),
        "type": itype,
        "collectionType": ctype,
        "seriesId": _ns(str(i["SeriesId"])) if i.get("SeriesId") else None,
        "seriesName": i.get("SeriesName") or None,
    }


# ── watch summary ─────────────────────────────────────────────────────
def watch_summary(parent_raw, kind):
    accts = _accounts_full()
    if not accts:
        return {}
    all_account_ids = {a["id"] for a in accts}

    jf_type = "Movie" if kind == "movies" else "Series"
    data = jellyfin_get("/Items", {"ParentId": parent_raw, "IncludeItemTypes": jf_type,
                                   "Recursive": "true", "fields": "ProviderIds", "Limit": 10000})
    all_items = data.get("Items", [])
    if not all_items:
        return {}

    db = get_db()
    item_assignments, provider_assignments = _load_assignment_maps(db)

    def resolve_target(providers, item_id):
        return _resolve_target(providers, item_id, item_assignments, provider_assignments, all_account_ids)

    has_pwe = pwe_has_data(db)
    result = {}
    if has_pwe:
        if kind == "movies":
            played = pwe_get_played(db, list(all_account_ids), ["movie"])
            played_rk = pwe_get_played_by_ratingkey(db, list(all_account_ids), ["movie"])
            for i in all_items:
                iid = _ns(str(i["Id"]))
                providers = parse_jellyfin_providers(i.get("ProviderIds") or {})
                target_ids = resolve_target(providers, iid)
                if not target_ids:
                    result[iid] = {"watched": 0, "total": 0, "watcher_ids": []}
                    continue
                watched_count = 0
                watcher_ids = []
                rk_watchers = played_rk.get(iid, set())
                for aid in target_ids:
                    saw_it = (bool(providers) and is_item_watched_pwe(providers, "movie", {aid}, played)) or (aid in rk_watchers)
                    if saw_it:
                        watched_count += 1
                        watcher_ids.append(aid)
                result[iid] = {"watched": watched_count, "total": len(target_ids), "watcher_ids": watcher_ids}
        else:
            ep_data = jellyfin_get("/Items", {"ParentId": parent_raw, "IncludeItemTypes": "Episode",
                                              "Recursive": "true", "fields": "ProviderIds,SeriesId", "Limit": 50000})
            show_eps = {}
            for ep in ep_data.get("Items", []):
                show_id = _ns(str(ep.get("SeriesId", ""))) if ep.get("SeriesId") else ""
                ep_id = _ns(str(ep.get("Id", ""))) if ep.get("Id") else ""
                providers = parse_jellyfin_providers(ep.get("ProviderIds") or {})
                if show_id and (providers or ep_id):
                    show_eps.setdefault(show_id, []).append((providers, ep_id))
            ep_played = pwe_get_played(db, list(all_account_ids), ["episode"])
            ep_played_rk = pwe_get_played_by_ratingkey(db, list(all_account_ids), ["episode"])
            for i in all_items:
                iid = _ns(str(i["Id"]))
                show_providers = parse_jellyfin_providers(i.get("ProviderIds") or {})
                target_ids = resolve_target(show_providers, iid)
                eps = show_eps.get(iid, [])
                if not target_ids or not eps:
                    result[iid] = {"watched": 0, "total": len(target_ids), "watcher_ids": []}
                    continue
                watched_count = 0
                watcher_ids = []
                for aid in target_ids:
                    saw_all = all(
                        (bool(p) and is_item_watched_pwe(p, "episode", {aid}, ep_played)) or
                        (rk and aid in ep_played_rk.get(rk, set()))
                        for p, rk in eps)
                    if saw_all:
                        watched_count += 1
                        watcher_ids.append(aid)
                result[iid] = {"watched": watched_count, "total": len(target_ids), "watcher_ids": watcher_ids}
    else:
        for i in all_items:
            iid = _ns(str(i["Id"]))
            providers = parse_jellyfin_providers(i.get("ProviderIds") or {})
            target_ids = resolve_target(providers, iid)
            result[iid] = {"watched": 0, "total": len(target_ids), "watcher_ids": []}
    db.close()
    return result


def watch_summary_items(movie_raws, ep_raws):
    accts = _accounts_full()
    if not accts:
        return {}
    all_account_ids = {a["id"] for a in accts}

    db = get_db()
    item_assignments, provider_assignments = _load_assignment_maps(db)
    has_pwe = pwe_has_data(db)

    def resolve_target(providers, item_id):
        return _resolve_target(providers, item_id, item_assignments, provider_assignments, all_account_ids)

    result = {}
    if not has_pwe:
        db.close()
        for raw in movie_raws + ep_raws:
            result[_ns(raw)] = {"watched": 0, "total": 0, "watcher_ids": []}
        return result

    played = pwe_get_played(db, list(all_account_ids), ["movie", "episode"])
    db.close()

    items_by_id = {}
    all_ids = list({*movie_raws, *ep_raws})
    if all_ids:
        try:
            for it in jellyfin_items({"Ids": ",".join(all_ids), "fields": "ProviderIds",
                                      "Recursive": "true", "Limit": len(all_ids)}):
                iid = str(it.get("Id") or "")
                if iid:
                    items_by_id[iid] = it
        except Exception:
            pass

    series_ids = list({str(items_by_id.get(eid, {}).get("SeriesId") or "")
                       for eid in ep_raws if items_by_id.get(eid, {}).get("SeriesId")})
    series_providers = {}
    if series_ids:
        try:
            for it in jellyfin_items({"Ids": ",".join(series_ids), "fields": "ProviderIds",
                                      "Recursive": "true", "Limit": len(series_ids)}):
                sid = str(it.get("Id") or "")
                if sid:
                    series_providers[sid] = parse_jellyfin_providers(it.get("ProviderIds") or {})
        except Exception:
            pass

    for raw in movie_raws:
        it = items_by_id.get(raw)
        iid = _ns(raw)
        if not it:
            result[iid] = {"watched": 0, "total": 0, "watcher_ids": []}
            continue
        providers = parse_jellyfin_providers(it.get("ProviderIds") or {})
        target_ids = resolve_target(providers, iid)
        watched_count = 0
        watcher_ids = []
        for aid in target_ids:
            if bool(providers) and is_item_watched_pwe(providers, "movie", {aid}, played):
                watched_count += 1
                watcher_ids.append(aid)
        result[iid] = {"watched": watched_count, "total": len(target_ids), "watcher_ids": watcher_ids}

    for raw in ep_raws:
        it = items_by_id.get(raw)
        iid = _ns(raw)
        if not it:
            result[iid] = {"watched": 0, "total": 0, "watcher_ids": []}
            continue
        providers = parse_jellyfin_providers(it.get("ProviderIds") or {})
        series_id_raw = str(it.get("SeriesId") or "")
        show_providers = series_providers.get(series_id_raw, {}) if series_id_raw else {}
        target_ids = resolve_target(show_providers, _ns(series_id_raw) if series_id_raw else "")
        watched_count = 0
        watcher_ids = []
        for aid in target_ids:
            saw_it = is_item_watched_pwe(providers, "episode", {aid}, played) if providers else False
            if saw_it:
                watched_count += 1
                watcher_ids.append(aid)
        result[iid] = {"watched": watched_count, "total": len(target_ids), "watcher_ids": watcher_ids}
    return result


# ── seasons / episodes / watch status ─────────────────────────────────
def seasons(series_raw):
    seasons_raw = jellyfin_items({"ParentId": series_raw, "IncludeItemTypes": "Season",
                                  "SortBy": "IndexNumber", "fields": "ChildCount"})
    if not seasons_raw:
        return []
    accts = _accounts_full()
    assigned_ids = get_assigned_ids(_ns(series_raw))
    target_accounts = accts if assigned_ids is None else [a for a in accts if a["id"] in assigned_ids]
    if not target_accounts:
        target_accounts = accts

    db = get_db()
    has_pwe = pwe_has_data(db)
    if has_pwe:
        target_ids = [a["id"] for a in target_accounts]
        played = pwe_get_played(db, target_ids, ["episode"])
        played_rk = pwe_get_played_by_ratingkey(db, target_ids, ["episode"])
        db.close()
        try:
            all_eps = jellyfin_items({"ParentId": series_raw, "Recursive": "true",
                                      "IncludeItemTypes": "Episode", "fields": "ProviderIds", "SortBy": "IndexNumber"})
        except Exception:
            all_eps = []
        season_eps = {}
        for ep in all_eps:
            parent_id = str(ep.get("SeasonId") or ep.get("ParentId") or "")
            if parent_id:
                season_eps.setdefault(parent_id, []).append(
                    (_ns(str(ep["Id"])), parse_jellyfin_providers(ep.get("ProviderIds") or {})))
        result = []
        for s in seasons_raw:
            sid_raw = str(s["Id"])
            total = s.get("ChildCount") or 0
            eps = season_eps.get(sid_raw, [])
            if not total and eps:
                total = len(eps)
            completed_users, per_user = 0, []
            for acc in target_accounts:
                if eps:
                    t = len(eps)
                    played_count = sum(
                        1 for ep_id, providers in eps
                        if (providers and is_item_watched_pwe(providers, "episode", {acc["id"]}, played))
                        or (ep_id and acc["id"] in played_rk.get(ep_id, set())))
                    completed = t > 0 and played_count >= t
                else:
                    played_count, t, completed = 0, total, False
                if completed:
                    completed_users += 1
                per_user.append({"userId": acc["id"], "userName": acc["name"],
                                 "playedCount": played_count, "totalCount": t, "completed": completed})
            result.append({"id": _ns(sid_raw), "name": s.get("Name", ""), "indexNumber": s.get("IndexNumber", 0),
                           "totalEpisodes": total, "userProgress": per_user,
                           "completedUsers": completed_users, "totalAssignedUsers": len(target_accounts)})
        return result

    db.close()
    result = []
    for s in seasons_raw:
        sid_raw = str(s["Id"])
        total = s.get("ChildCount") or 0
        completed_users, per_user = 0, []
        for acc in target_accounts:
            try:
                played_eps = jellyfin_items({"ParentId": sid_raw, "IncludeItemTypes": "Episode",
                                             "IsPlayed": "true", "userId": acc["raw"]})
                played_count = len(played_eps)
            except Exception:
                played_count = 0
            completed = total > 0 and played_count >= total
            if completed:
                completed_users += 1
            per_user.append({"userId": acc["id"], "userName": acc["name"],
                             "playedCount": played_count, "totalCount": total, "completed": completed})
        result.append({"id": _ns(sid_raw), "name": s.get("Name", ""), "indexNumber": s.get("IndexNumber", 0),
                       "totalEpisodes": total, "userProgress": per_user,
                       "completedUsers": completed_users, "totalAssignedUsers": len(target_accounts)})
    return result


def season_watch_status(series_raw, season_raw):
    accts = _accounts_full()
    db = get_db()
    has_pwe = pwe_has_data(db)
    episodes_raw = jellyfin_items({"ParentId": season_raw, "IncludeItemTypes": "Episode",
                                   "fields": "ProviderIds", "SortBy": "IndexNumber"})
    if has_pwe:
        all_ids = [a["id"] for a in accts]
        played = pwe_get_played(db, all_ids, ["episode"])
        played_ts = pwe_get_played_with_ts(db, all_ids, ["episode"])
        played_rk = pwe_get_played_by_ratingkey(db, all_ids, ["episode"])
        db.close()
        result = []
        for ep in episodes_raw:
            eid = _ns(str(ep["Id"]))
            providers = parse_jellyfin_providers(ep.get("ProviderIds") or {})
            rk_watchers = played_rk.get(eid, set())
            users = []
            for acc in accts:
                is_played = (bool(providers) and is_item_watched_pwe(providers, "episode", {acc["id"]}, played)) or (acc["id"] in rk_watchers)
                last_ts = get_last_played_pwe(providers, "episode", acc["id"], played_ts) if providers else None
                users.append({"userId": acc["id"], "userName": acc["name"], "played": is_played,
                              "playCount": 1 if is_played else 0, "lastPlayedDate": last_ts,
                              "playedPercentage": 100.0 if is_played else 0.0})
            result.append({"id": eid, "name": ep.get("Name", ""), "indexNumber": ep.get("IndexNumber", 0),
                           "runTimeTicks": ep.get("RunTimeTicks") or 0, "users": users})
        return result

    db.close()
    result = []
    for ep in episodes_raw:
        eid_raw = str(ep["Id"])
        runtime_ticks = ep.get("RunTimeTicks") or 0
        users = []
        for acc in accts:
            try:
                ud_data = jellyfin_get_item(eid_raw, {"fields": "UserData", "userId": acc["raw"]})
                ud = ud_data.get("UserData") or {}
                is_played = bool(ud.get("Played", False))
                play_count = ud.get("PlayCount") or (1 if is_played else 0)
                last_ts = ud.get("LastPlayedDate")
                pos_ticks = ud.get("PlaybackPositionTicks") or 0
                pct = 100.0 if is_played else (round(pos_ticks / runtime_ticks * 100, 1) if runtime_ticks else 0)
            except Exception:
                is_played, play_count, last_ts, pct = False, 0, None, 0.0
            users.append({"userId": acc["id"], "userName": acc["name"], "played": is_played,
                          "playCount": play_count, "lastPlayedDate": last_ts, "playedPercentage": pct})
        result.append({"id": _ns(eid_raw), "name": ep.get("Name", ""), "indexNumber": ep.get("IndexNumber", 0),
                       "runTimeTicks": runtime_ticks, "users": users})
    return result


def watch_status(raw):
    accts = _accounts_full()
    db = get_db()
    has_pwe = pwe_has_data(db)
    item_data = jellyfin_get_item(raw, {"fields": "ProviderIds"})
    raw_type = item_data.get("Type", "Movie")
    providers = parse_jellyfin_providers(item_data.get("ProviderIds") or {})
    all_ids = [a["id"] for a in accts]

    if has_pwe:
        if raw_type == "Series":
            try:
                all_eps = jellyfin_items({"ParentId": raw, "Recursive": "true",
                                          "IncludeItemTypes": "Episode", "fields": "ProviderIds"})
                eps = [(_ns(str(e.get("Id") or "")) if e.get("Id") else "",
                        parse_jellyfin_providers(e.get("ProviderIds") or {})) for e in all_eps]
                eps = [(eid, p) for eid, p in eps if eid or p]
            except Exception:
                eps = []
            ep_played = pwe_get_played(db, all_ids, ["episode"])
            ep_played_ts = pwe_get_played_with_ts(db, all_ids, ["episode"])
            ep_played_rk = pwe_get_played_by_ratingkey(db, all_ids, ["episode"])
            db.close()
            out = []
            for acc in accts:
                is_played = bool(eps) and all(
                    (bool(p) and is_item_watched_pwe(p, "episode", {acc["id"]}, ep_played))
                    or (eid and acc["id"] in ep_played_rk.get(eid, set()))
                    for eid, p in eps)
                last_ts = None
                if is_played and eps:
                    for _, p in eps:
                        if not p:
                            continue
                        t = get_last_played_pwe(p, "episode", acc["id"], ep_played_ts)
                        if t and (last_ts is None or t > last_ts):
                            last_ts = t
                out.append({"userId": acc["id"], "userName": acc["name"], "played": is_played,
                            "playCount": 1 if is_played else 0, "lastPlayedDate": last_ts,
                            "playedPercentage": 100.0 if is_played else 0.0})
        else:
            ws_itype = "episode" if raw_type == "Episode" else "movie"
            played = pwe_get_played(db, all_ids, [ws_itype])
            played_ts = pwe_get_played_with_ts(db, all_ids, [ws_itype])
            played_rk = pwe_get_played_by_ratingkey(db, all_ids, [ws_itype])
            db.close()
            rk_watchers = played_rk.get(_ns(raw), set())
            out = []
            for acc in accts:
                is_played = (bool(providers) and is_item_watched_pwe(providers, ws_itype, {acc["id"]}, played)) or (acc["id"] in rk_watchers)
                last_ts = get_last_played_pwe(providers, ws_itype, acc["id"], played_ts) if providers else None
                out.append({"userId": acc["id"], "userName": acc["name"], "played": is_played,
                            "playCount": 1 if is_played else 0, "lastPlayedDate": last_ts,
                            "playedPercentage": 100.0 if is_played else 0.0})
        return out

    db.close()
    out = []
    for acc in accts:
        try:
            ud_data = jellyfin_get_item(raw, {"fields": "UserData", "userId": acc["raw"]})
            ud = ud_data.get("UserData") or {}
            is_played = bool(ud.get("Played", False))
            play_count = ud.get("PlayCount") or (1 if is_played else 0)
            last_ts = ud.get("LastPlayedDate")
            pos_ticks = ud.get("PlaybackPositionTicks") or 0
            rt = ud_data.get("RunTimeTicks") or 1
            pct = 100.0 if is_played else round(min(pos_ticks / rt * 100, 99.9), 1)
        except Exception:
            is_played, play_count, last_ts, pct = False, 0, None, 0.0
        out.append({"userId": acc["id"], "userName": acc["name"], "played": is_played,
                    "playCount": play_count, "lastPlayedDate": last_ts, "playedPercentage": pct})
    return out


# ── recent / activity ─────────────────────────────────────────────────
def _admin_recent_items(admin_id, include_type, fields, monitored_raw, show_all):
    base = {"userId": admin_id, "IsPlayed": "true", "Recursive": "true",
            "IncludeItemTypes": include_type, "SortBy": "DatePlayed",
            "SortOrder": "Descending", "Limit": 100, "fields": fields}
    if show_all or not monitored_raw:
        try:
            return jellyfin_items(base)
        except Exception:
            return []
    seen = {}
    for lib_id in monitored_raw:
        try:
            items = jellyfin_items({**base, "parentId": lib_id})
        except Exception:
            continue
        for it in items:
            rid = str(it.get("Id") or "")
            if rid:
                seen.setdefault(rid, it)
    return list(seen.values())


def _bulk_items_by_ids(rating_keys, include_type, fields, monitored_raw, show_all):
    rks = [str(rk) for rk in rating_keys if rk]
    if not rks:
        return {}
    base = {"Ids": ",".join(rks), "IncludeItemTypes": include_type,
            "Recursive": "true", "fields": fields, "Limit": len(rks)}
    out = {}
    parents = [None] if (show_all or not monitored_raw) else list(monitored_raw)
    for parent in parents:
        params = base if parent is None else {**base, "parentId": parent}
        try:
            items = jellyfin_items(params)
        except Exception:
            continue
        for it in items:
            rid = str(it.get("Id") or "")
            if rid:
                out.setdefault(rid, it)
    return out


def _monitored_raw(cfg):
    """Raw library ids of this engine's monitored libraries (from namespaced config)."""
    out = []
    for nsid in cfg.get("monitored_libraries", []):
        k, raw = idutil.split_id(nsid)
        if k == KEY:
            out.append(raw)
    return out


def recent(kind, cfg):
    monitored_raw = _monitored_raw(cfg)
    show_all = cfg.get("show_all_libraries", True)
    admin_id = jellyfin_admin_id()
    result = {}
    include_type = "Movie" if kind == "movies" else "Episode"
    db_item_type = "movie" if kind == "movies" else "episode"

    if admin_id:
        fields = "ProviderIds,ParentId,UserData" if kind == "movies" else "ProviderIds,UserData"
        for i in _admin_recent_items(admin_id, include_type, fields, monitored_raw, show_all):
            rid = str(i.get("Id") or "")
            if not rid:
                continue
            lv = (i.get("UserData") or {}).get("LastPlayedDate")
            if not lv:
                continue
            nsid = _ns(rid)
            if kind == "movies":
                result[nsid] = {"id": nsid, "name": i.get("Name", ""), "year": i.get("ProductionYear"),
                                "imageUrl": f"/api/image/{nsid}?type=Primary&maxWidth=200", "lastPlayedDate": lv}
            else:
                series_id = str(i.get("SeriesId") or "")
                img = _ns(series_id) if series_id else nsid
                result[nsid] = {"id": nsid, "name": i.get("Name", ""), "seriesName": i.get("SeriesName", ""),
                                "seasonName": i.get("SeasonName", ""), "episodeNumber": i.get("IndexNumber"),
                                "imageUrl": f"/api/image/{img}?type=Primary&maxWidth=200",
                                "lastPlayedDate": lv, "seriesId": _ns(series_id) if series_id else ""}

    db = get_db()
    rows = db.execute(
        "SELECT rating_key, MAX(updated_at) as latest_at FROM watch_events "
        "WHERE event_type='play' AND item_type=? AND rating_key != '' "
        "GROUP BY rating_key ORDER BY latest_at DESC LIMIT 200", (db_item_type,)).fetchall()
    db.close()

    # rating_key is namespaced in storage; only our own rows are relevant here.
    ts_by_ns = {}
    for r in rows:
        k, raw = idutil.split_id(r["rating_key"])
        if k != KEY:
            continue
        nsid = r["rating_key"]
        if (result.get(nsid, {}).get("lastPlayedDate") or "") < r["latest_at"]:
            ts_by_ns[nsid] = (raw, r["latest_at"])

    items = _bulk_items_by_ids([v[0] for v in ts_by_ns.values()], include_type, "ProviderIds", monitored_raw, show_all)
    for nsid, (raw, ts) in ts_by_ns.items():
        i = items.get(raw)
        if not i:
            continue
        if kind == "movies":
            result[nsid] = {"id": nsid, "name": i.get("Name", ""), "year": i.get("ProductionYear"),
                            "imageUrl": f"/api/image/{nsid}?type=Primary&maxWidth=200", "lastPlayedDate": ts}
        else:
            series_id = str(i.get("SeriesId") or "")
            img = _ns(series_id) if series_id else nsid
            result[nsid] = {"id": nsid, "name": i.get("Name", ""), "seriesName": i.get("SeriesName", ""),
                            "seasonName": i.get("SeasonName", ""), "episodeNumber": i.get("IndexNumber"),
                            "imageUrl": f"/api/image/{img}?type=Primary&maxWidth=200",
                            "lastPlayedDate": ts, "seriesId": _ns(series_id) if series_id else ""}
    return list(result.values())


def activity():
    try:
        sessions = jellyfin_get("/Sessions")
    except Exception:
        return []
    out = []
    for i in sessions:
        npi = i.get("NowPlayingItem") or {}
        if not npi:
            continue
        rid = str(npi.get("Id", ""))
        ps = i.get("PlayState") or {}
        pos_ticks = ps.get("PositionTicks") or 0
        rt_ticks = npi.get("RunTimeTicks") or 1
        progress = round(min(pos_ticks / rt_ticks * 100, 100), 1)
        play_method = (ps.get("PlayMethod") or "").lower()
        stream_type = "Direct Play" if play_method == "directplay" else ("Transcode" if play_method == "transcode" else "")
        series_id = str(npi.get("SeriesId") or "")
        img = _ns(series_id) if series_id else (_ns(rid) if rid else None)
        out.append({
            "id": _ns(str(i.get("Id", rid))), "type": npi.get("Type", "").lower(), "provider": KEY,
            "title": npi.get("Name", ""), "seriesName": npi.get("SeriesName") or None,
            "seasonName": npi.get("SeasonName") or None, "episodeNumber": npi.get("IndexNumber"),
            "imageUrl": f"/api/image/{img}?type=Primary&maxWidth=300" if img else None,
            "progress": progress, "viewOffset": pos_ticks // 10000, "duration": rt_ticks // 10000,
            "user": i.get("UserName", "Unknown"), "player": i.get("DeviceName", ""),
            "streamType": stream_type, "bandwidth": None,
            "ratingKey": _ns(rid) if rid else "",
            "grandparentRatingKey": _ns(series_id) if series_id else "",
            "parentRatingKey": _ns(str(npi.get("SeasonId"))) if npi.get("SeasonId") else "",
            "librarySectionID": _ns(str(npi.get("ParentId"))) if npi.get("ParentId") else "",
        })
    return out


# ── backfill ──────────────────────────────────────────────────────────
def backfill():
    accts = jellyfin_all_users()
    if not accts:
        return {"ok": False, "error": "no accounts", "imported": 0, "total_history": 0}
    db = get_db()
    imported = 0
    total_history = 0
    try:
        for acc in accts:
            uid_raw = acc["id"]
            uid_ns = _ns(uid_raw)
            try:
                items = jellyfin_items({"userId": uid_raw, "IsPlayed": "true", "Recursive": "true",
                                        "IncludeItemTypes": "Movie,Episode",
                                        "fields": "ProviderIds,UserData", "Limit": 10000})
            except Exception:
                continue
            for item in items:
                raw_type = item.get("Type", "")
                if raw_type not in ("Movie", "Episode"):
                    continue
                rk = str(item.get("Id", ""))
                providers = parse_jellyfin_providers(item.get("ProviderIds") or {})
                if not rk or not providers:
                    continue
                item_type = "movie" if raw_type == "Movie" else "episode"
                ud = item.get("UserData") or {}
                iso_ts = ud.get("LastPlayedDate") or datetime.now(timezone.utc).isoformat()
                total_history += 1
                for ptype, pid in providers.items():
                    cur = db.execute(
                        "INSERT OR IGNORE INTO watch_events "
                        "(account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key) "
                        "VALUES (?, ?, ?, ?, 'play', ?, ?)",
                        (uid_ns, ptype.lower(), str(pid), item_type, iso_ts, _ns(rk)))
                    imported += cur.rowcount
        db.commit()
    finally:
        db.close()
    return {"ok": True, "imported": imported, "total_history": total_history}


# ── webhook ───────────────────────────────────────────────────────────
def store_webhook(request, cfg):
    """Full Jellyfin webhook handler. Returns (json_dict, status_code)."""
    data = request.get_json(force=True, silent=True) or {}
    notification_type = data.get("NotificationType", "")
    if notification_type == "PlaybackStop":
        if not data.get("PlayedToCompletion"):
            return {"ok": True, "skipped": "not_played_to_completion"}, 200
    elif notification_type == "UserDataSaved":
        played_field = data.get("Played")
        if isinstance(played_field, str):
            played_field = played_field.strip().lower() == "true"
        if not played_field:
            return {"ok": True, "skipped": "user_data_saved_not_played"}, 200
    else:
        return {"ok": True, "skipped": notification_type}, 200

    account_raw = data.get("UserId")
    if not account_raw:
        return {"error": "no UserId"}, 400
    account_ns = _ns(str(account_raw))

    raw_type = (data.get("ItemType") or "").lower()
    type_map = {"movie": "movie", "episode": "episode"}
    item_type = type_map.get(raw_type)
    if not item_type:
        return {"ok": True, "skipped": f"type:{raw_type}"}, 200

    rk_raw = str(data.get("ItemId") or "")
    rk_ns = _ns(rk_raw) if rk_raw else ""
    providers_raw = {}
    if data.get("Provider_tmdb"):
        providers_raw["Tmdb"] = str(data["Provider_tmdb"])
    if data.get("Provider_tvdb"):
        providers_raw["Tvdb"] = str(data["Provider_tvdb"])
    if data.get("Provider_imdb"):
        providers_raw["Imdb"] = str(data["Provider_imdb"])
    providers = parse_jellyfin_providers(providers_raw)
    if not providers and not rk_ns:
        return {"ok": True, "skipped": "no provider ids or item id"}, 200

    meta = {
        "Name": data.get("Name", ""), "type": item_type, "librarySectionID": None,
        "SeriesId": data.get("SeriesId") or "",
        "ProviderIds": providers_raw,
        "UserData": {"Played": True, "LastPlayedDate": data.get("UtcTimestamp") or data.get("Timestamp")},
    }

    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    stored = 0
    for ptype, pid in providers.items():
        db.execute(
            "INSERT INTO watch_events (account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key) "
            "VALUES (?, ?, ?, ?, 'play', ?, ?) "
            "ON CONFLICT(account_id, provider_type, provider_id, item_type) "
            "DO UPDATE SET event_type=excluded.event_type, updated_at=excluded.updated_at, rating_key=excluded.rating_key",
            (account_ns, ptype.lower(), str(pid), item_type, now, rk_ns))
        stored += 1
    if stored == 0 and rk_ns:
        db.execute(
            "INSERT INTO watch_events (account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key) "
            "VALUES (?, 'ratingkey', ?, ?, 'play', ?, ?) "
            "ON CONFLICT(account_id, provider_type, provider_id, item_type) "
            "DO UPDATE SET event_type=excluded.event_type, updated_at=excluded.updated_at, rating_key=excluded.rating_key",
            (account_ns, rk_ns, item_type, now, rk_ns))
        stored = 1
    db.commit()
    logger.info(f"Jellyfin webhook: account={account_ns} type={item_type} providers={providers} item_id={rk_raw}")
    if item_type in ("movie", "episode"):
        try:
            maybe_auto_delete(db, cfg, rk_raw, item_type, providers, meta)
        except Exception as e:
            logger.warning(f"Auto-delete check error for {rk_raw}: {e}")
    db.close()
    return {"ok": True, "stored": stored}, 200


# ── delete / image / misc ─────────────────────────────────────────────
def delete(raw):
    jellyfin_delete(raw)


def check_season_empty(season_raw):
    try:
        episodes = jellyfin_items({"ParentId": season_raw, "IncludeItemTypes": "Episode", "Limit": 1})
        return len(episodes) == 0
    except Exception:
        return False


def image_response(raw, w_int, img_type):
    if img_type not in ("Primary", "Backdrop", "Thumb", "Banner", "Logo"):
        img_type = "Primary"
    try:
        url = f"{get_jellyfin_url()}/Items/{raw}/Images/{img_type}"
        r = http_requests.get(url, headers=_jf_headers(),
                              params={"maxWidth": str(w_int), "quality": "90"}, timeout=15, stream=True)
        if r.status_code == 404:
            url = f"{get_jellyfin_url()}/Items/{raw}/Images/Primary"
            r = http_requests.get(url, headers=_jf_headers(),
                                  params={"maxWidth": str(w_int), "quality": "90"}, timeout=15, stream=True)
        r.raise_for_status()
        return Response(r.iter_content(8192), content_type=r.headers.get("Content-Type", "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        return Response(status=404)


# ── auto-delete ───────────────────────────────────────────────────────
def _owner_state(item_meta):
    """(owner_saw, owner_ts) from a Jellyfin item's UserData."""
    ud = (item_meta or {}).get("UserData") or {}
    saw = bool(ud.get("Played", False))
    return saw, (ud.get("LastPlayedDate") if saw else None)


def maybe_auto_delete(db, cfg, rk_raw, item_type, providers, meta):
    if not cfg.get("auto_delete_enabled") or not rk_raw:
        return
    library_raw = str(meta.get("librarySectionID") or "")
    series_raw = str(meta.get("SeriesId") or "") if item_type == "episode" else ""
    if not library_raw:
        try:
            d = jellyfin_get_item(rk_raw, {"fields": "ParentId"})
            library_raw = str(d.get("ParentId") or "")
            if item_type == "episode" and not series_raw:
                series_raw = str(d.get("SeriesId") or "")
        except Exception:
            pass
    if not library_raw:
        logger.warning(f"Auto-delete: cannot resolve library for {rk_raw}, skipping")
        return
    lib_ns = _ns(library_raw)
    series_ns = _ns(series_raw) if series_raw else None
    rk_ns = _ns(rk_raw)
    movie_ns = rk_ns if item_type == "movie" else None
    if not adc.is_auto_delete_active(db, cfg, lib_ns, series_ns, movie_ns):
        return
    all_account_ids = {a["id"] for a in _accounts_full()}
    if not all_account_ids:
        return
    item_assignments, provider_assignments = _load_assignment_maps(db)
    lookup_id = series_ns if (item_type == "episode" and series_ns) else rk_ns
    target_ids = _resolve_target(providers if item_type == "movie" else {}, lookup_id,
                                 item_assignments, provider_assignments, all_account_ids)
    if not target_ids:
        return
    grace_days = int(cfg.get("auto_delete_grace_days") or 0)
    min_delay = int(cfg.get("auto_delete_min_delay_minutes") or 30)
    enabled_since = adc.get_enabled_since(db, cfg, lib_ns, series_ns, movie_ns)
    owner_saw, owner_ts = _owner_state(meta)
    if not adc.candidate_ok(db, rk_ns, item_type, providers, target_ids, grace_days, enabled_since,
                            owner_id=owner_id(), owner_saw=owner_saw, owner_ts=owner_ts, min_delay_minutes=min_delay):
        return
    if grace_days > 0 or min_delay > 0:
        return
    try:
        title = str(meta.get("Name") or rk_raw)
        jellyfin_delete(rk_raw)
        logger.info(f"Auto-deleted {item_type} {rk_raw} '{title}' — all assigned users watched")
    except Exception as e:
        logger.warning(f"Auto-delete failed for {rk_raw}: {e}")


def run_sweep(cfg):
    all_account_ids = {a["id"] for a in _accounts_full()}
    if not all_account_ids:
        return 0
    own = owner_id()
    admin_raw = jellyfin_admin_id()
    db = get_db()
    deleted = 0
    try:
        item_assignments, provider_assignments = _load_assignment_maps(db)
        grace_days = int(cfg.get("auto_delete_grace_days") or 0)
        min_delay = int(cfg.get("auto_delete_min_delay_minutes") or 30)
        auto_libs = [nsid for nsid in (cfg.get("auto_delete_libraries") or []) if idutil.key_of(nsid) == KEY]
        force_series = [r["scope_id"] for r in db.execute(
            "SELECT scope_id FROM auto_delete_overrides WHERE scope='series' AND enabled=1").fetchall()
            if idutil.key_of(r["scope_id"]) == KEY]
        force_movies = [r["scope_id"] for r in db.execute(
            "SELECT scope_id FROM auto_delete_overrides WHERE scope='movie' AND enabled=1").fetchall()
            if idutil.key_of(r["scope_id"]) == KEY]
        section_map = {str(s["Id"]): s for s in jellyfin_libraries()}

        for lib_ns in auto_libs:
            lib_raw = idutil.raw_of(lib_ns)
            section = section_map.get(lib_raw)
            if not section:
                continue
            lib_type = (section.get("CollectionType") or "").lower()
            try:
                if lib_type == "movies":
                    items = jellyfin_items({"ParentId": lib_raw, "Recursive": "true", "IncludeItemTypes": "Movie",
                                            "fields": "ProviderIds,UserData", "userId": admin_raw, "Limit": 10000})
                    for i in items:
                        rk_raw = str(i.get("Id", ""))
                        if not rk_raw:
                            continue
                        rk_ns = _ns(rk_raw)
                        if not adc.is_auto_delete_active(db, cfg, lib_ns, movie_id=rk_ns):
                            continue
                        providers = parse_jellyfin_providers(i.get("ProviderIds") or {})
                        target_ids = _resolve_target(providers, rk_ns, item_assignments, provider_assignments, all_account_ids)
                        if not target_ids:
                            continue
                        enabled_since = adc.get_enabled_since(db, cfg, lib_ns, movie_id=rk_ns)
                        owner_saw, owner_ts = _owner_state(i)
                        if not adc.candidate_ok(db, rk_ns, "movie", providers, target_ids, grace_days, enabled_since,
                                                owner_id=own, owner_saw=owner_saw, owner_ts=owner_ts, min_delay_minutes=min_delay):
                            continue
                        try:
                            jellyfin_delete(rk_raw)
                            logger.info(f"Auto-deleted movie {rk_raw} '{i.get('Name', rk_raw)}' (sweep lib)")
                            deleted += 1
                        except Exception as e:
                            logger.warning(f"Sweep auto-delete failed movie {rk_raw}: {e}")
                        time.sleep(0.1)
                elif lib_type == "tvshows":
                    episodes = jellyfin_items({"ParentId": lib_raw, "Recursive": "true", "IncludeItemTypes": "Episode",
                                               "fields": "ProviderIds,UserData,SeriesId", "userId": admin_raw, "Limit": 50000})
                    show_providers_cache = {}
                    for ep in episodes:
                        rk_raw = str(ep.get("Id", ""))
                        series_raw = str(ep.get("SeriesId", ""))
                        if not rk_raw or not series_raw:
                            continue
                        series_ns = _ns(series_raw)
                        if not adc.is_auto_delete_active(db, cfg, lib_ns, series_ns):
                            continue
                        if series_raw not in show_providers_cache:
                            try:
                                s_data = jellyfin_get_item(series_raw, {"fields": "ProviderIds"})
                                show_providers_cache[series_raw] = parse_jellyfin_providers(s_data.get("ProviderIds") or {})
                            except Exception:
                                show_providers_cache[series_raw] = {}
                        target_ids = _resolve_target(show_providers_cache[series_raw], series_ns,
                                                     item_assignments, provider_assignments, all_account_ids)
                        if not target_ids:
                            continue
                        ep_providers = parse_jellyfin_providers(ep.get("ProviderIds") or {})
                        enabled_since = adc.get_enabled_since(db, cfg, lib_ns, series_ns)
                        owner_saw, owner_ts = _owner_state(ep)
                        if not adc.candidate_ok(db, _ns(rk_raw), "episode", ep_providers, target_ids, grace_days, enabled_since,
                                                owner_id=own, owner_saw=owner_saw, owner_ts=owner_ts, min_delay_minutes=min_delay):
                            continue
                        try:
                            jellyfin_delete(rk_raw)
                            logger.info(f"Auto-deleted episode {rk_raw} '{ep.get('Name', rk_raw)}' (sweep lib)")
                            deleted += 1
                        except Exception as e:
                            logger.warning(f"Sweep auto-delete failed episode {rk_raw}: {e}")
                        time.sleep(0.1)
            except Exception as e:
                logger.warning(f"Sweep error for library {lib_ns}: {e}")

        for series_ns in force_series:
            series_raw = idutil.raw_of(series_ns)
            try:
                try:
                    s_data = jellyfin_get_item(series_raw, {"fields": "ProviderIds"})
                    show_providers = parse_jellyfin_providers(s_data.get("ProviderIds") or {})
                except Exception:
                    show_providers = {}
                target_ids = _resolve_target(show_providers, series_ns, item_assignments, provider_assignments, all_account_ids)
                if not target_ids:
                    continue
                episodes = jellyfin_items({"ParentId": series_raw, "Recursive": "true", "IncludeItemTypes": "Episode",
                                           "fields": "ProviderIds,UserData", "userId": admin_raw, "Limit": 50000})
                for ep in episodes:
                    rk_raw = str(ep.get("Id", ""))
                    if not rk_raw:
                        continue
                    ep_providers = parse_jellyfin_providers(ep.get("ProviderIds") or {})
                    owner_saw, owner_ts = _owner_state(ep)
                    if not adc.candidate_ok(db, _ns(rk_raw), "episode", ep_providers, target_ids, grace_days,
                                            enabled_since=None, owner_id=own, owner_saw=owner_saw, owner_ts=owner_ts,
                                            min_delay_minutes=min_delay):
                        continue
                    try:
                        jellyfin_delete(rk_raw)
                        logger.info(f"Auto-deleted episode {rk_raw} (sweep force-on series {series_raw})")
                        deleted += 1
                    except Exception as e:
                        logger.warning(f"Sweep auto-delete failed episode {rk_raw}: {e}")
                    time.sleep(0.1)
            except Exception as e:
                logger.warning(f"Sweep error for force-on series {series_ns}: {e}")

        for movie_ns in force_movies:
            movie_raw = idutil.raw_of(movie_ns)
            try:
                m = jellyfin_get_item(movie_raw, {"fields": "ProviderIds,UserData"})
                providers = parse_jellyfin_providers(m.get("ProviderIds") or {})
                target_ids = _resolve_target(providers, movie_ns, item_assignments, provider_assignments, all_account_ids)
                if not target_ids:
                    continue
                owner_saw, owner_ts = _owner_state(m)
                if not adc.candidate_ok(db, movie_ns, "movie", providers, target_ids, grace_days,
                                        enabled_since=None, owner_id=own, owner_saw=owner_saw, owner_ts=owner_ts,
                                        min_delay_minutes=min_delay):
                    continue
                try:
                    jellyfin_delete(movie_raw)
                    logger.info(f"Auto-deleted movie {movie_raw} (sweep force-on movie)")
                    deleted += 1
                except Exception as e:
                    logger.warning(f"Sweep auto-delete failed movie {movie_raw}: {e}")
            except Exception as e:
                logger.warning(f"Sweep error for force-on movie {movie_ns}: {e}")
    finally:
        db.close()
    return deleted


def auto_delete_status(scope, raw, cfg):
    """GET status for a series/movie auto-delete toggle. scope in ('series','movie')."""
    ns = _ns(raw)
    db = get_db()
    row = db.execute("SELECT enabled FROM auto_delete_overrides WHERE scope=? AND scope_id=?", (scope, ns)).fetchone()
    db.close()
    override = None if row is None else bool(row["enabled"])
    library_ns = None
    try:
        d = jellyfin_get_item(raw, {"fields": "ParentId"})
        pid = str(d.get("ParentId") or "")
        library_ns = _ns(pid) if pid else None
    except Exception:
        pass
    library_enabled = bool(library_ns and library_ns in [str(x) for x in (cfg.get("auto_delete_libraries") or [])])
    effective = override if override is not None else library_enabled
    return {"override": override, "effective": effective, "library_enabled": library_enabled,
            "global_enabled": bool(cfg.get("auto_delete_enabled")),
            "grace_days": int(cfg.get("auto_delete_grace_days") or 0)}


def enabled_at_for_override(raw, db):
    """Compute enabled_at for a newly opted-in series/movie from Jellyfin UserData,
    so past watches count. Returns ISO string."""
    now_ts = datetime.now(timezone.utc).isoformat()
    ns = _ns(raw)
    try:
        params = {"fields": "UserData"}
        aid = jellyfin_admin_id()
        if aid:
            params["userId"] = aid
        item = jellyfin_get_item(raw, params)
        ud = item.get("UserData") or {}
        lva = ud.get("LastPlayedDate")
        played = ud.get("PlayCount") or (1 if ud.get("Played") else 0)
        if lva and played > 0:
            earliest = db.execute(
                "SELECT MIN(updated_at) as ts FROM watch_events WHERE rating_key=? AND event_type='play'",
                (ns,)).fetchone()
            watch_dt = datetime.fromisoformat(lva.replace("Z", "+00:00"))
            if earliest and earliest["ts"]:
                try:
                    watch_dt = min(watch_dt, datetime.fromisoformat(earliest["ts"].replace("Z", "+00:00")))
                except Exception:
                    pass
            return (watch_dt - timedelta(seconds=60)).isoformat()
    except Exception:
        pass
    return now_ts


def diagnose(raw, cfg):
    rating_key = _ns(raw)
    grace_days = int(cfg.get("auto_delete_grace_days") or 0)
    min_delay = int(cfg.get("auto_delete_min_delay_minutes") or 30)
    global_enabled = bool(cfg.get("auto_delete_enabled"))
    out = {"rating_key": rating_key, "global_enabled": global_enabled, "grace_days": grace_days,
           "min_delay_minutes": min_delay}
    if not global_enabled:
        out.update({"verdict": "GLOBAL_OFF", "verdict_detail": "auto_delete_enabled is False in Settings."})
        return out
    try:
        m = jellyfin_get_item(raw, {"fields": "ProviderIds,ParentId"})
        library_ns = _ns(str(m.get("ParentId"))) if m.get("ParentId") else ""
        rtype = m.get("Type", "")
        item_type = "episode" if rtype == "Episode" else ("movie" if rtype == "Movie" else rtype.lower() or "unknown")
        item_title = m.get("Name", raw)
        providers = parse_jellyfin_providers(m.get("ProviderIds") or {})
        series_ns = _ns(str(m.get("SeriesId"))) if (item_type == "episode" and m.get("SeriesId")) else None
    except Exception as e:
        out.update({"verdict": "PROVIDER_ERROR", "verdict_detail": f"Could not fetch metadata from Jellyfin: {e}"})
        return out
    out.update({"item_type": item_type, "item_title": item_title, "library_id": library_ns,
                "series_id": series_ns, "providers": providers})

    db = get_db()
    try:
        override_row = None
        if series_ns:
            override_row = db.execute("SELECT enabled FROM auto_delete_overrides WHERE scope='series' AND scope_id=?", (series_ns,)).fetchone()
        elif item_type == "movie":
            override_row = db.execute("SELECT enabled FROM auto_delete_overrides WHERE scope='movie' AND scope_id=?", (rating_key,)).fetchone()
        override = None if override_row is None else bool(override_row["enabled"])
        library_enabled = library_ns in [str(x) for x in (cfg.get("auto_delete_libraries") or [])]
        effective = override if override is not None else library_enabled
        out.update({"override": override, "library_enabled": library_enabled, "auto_delete_active": effective})
        if not effective:
            hint = "per-movie toggle" if item_type == "movie" else "per-show toggle"
            out.update({"verdict": "NOT_ACTIVE", "verdict_detail": f"Auto-delete is not active for this item. Enable it via the {hint} or opt the library in."})
            return out

        all_account_ids = {a["id"] for a in _accounts_full()}
        item_assignments, provider_assignments = _load_assignment_maps(db)
        lookup_id = series_ns if (item_type == "episode" and series_ns) else rating_key
        show_providers = providers if item_type == "movie" else {}
        if item_type == "episode" and series_ns:
            try:
                s_data = jellyfin_get_item(idutil.raw_of(series_ns), {"fields": "ProviderIds"})
                show_providers = parse_jellyfin_providers(s_data.get("ProviderIds") or {})
            except Exception:
                pass
        target_ids = _resolve_target(show_providers, lookup_id, item_assignments, provider_assignments, all_account_ids)
        out["target_users"] = sorted(target_ids)
        if not target_ids:
            out.update({"verdict": "NO_TARGETS", "verdict_detail": "No users assigned to this item."})
            return out

        played = pwe_get_played(db, list(target_ids), [item_type])
        played_rk = pwe_get_played_by_ratingkey(db, list(target_ids), [item_type])
        rk_watchers = played_rk.get(rating_key, set())
        watch_events = []
        all_watched = True
        latest_ts = None
        owner = owner_id()
        owner_played = bool((m.get("UserData") or {}).get("Played", False))
        for aid in target_ids:
            found_by = None
            if bool(providers) and is_item_watched_pwe(providers, item_type, {aid}, played):
                found_by = "provider"
            elif aid in rk_watchers:
                found_by = "rating_key"
            elif owner and aid == owner and owner_played:
                found_by = "userdata"
            else:
                all_watched = False
            user_ts = None
            if providers:
                for ptype, pid in providers.items():
                    row = db.execute(
                        "SELECT updated_at FROM watch_events WHERE account_id=? AND provider_type=? AND provider_id=? AND item_type=? AND event_type='play'",
                        (aid, ptype.lower(), str(pid), item_type)).fetchone()
                    if row and (user_ts is None or row["updated_at"] > user_ts):
                        user_ts = row["updated_at"]
            if user_ts is None:
                row = db.execute("SELECT updated_at FROM watch_events WHERE account_id=? AND rating_key=? AND event_type='play'", (aid, rating_key)).fetchone()
                if row:
                    user_ts = row["updated_at"]
            if user_ts is None and found_by == "userdata":
                user_ts = (m.get("UserData") or {}).get("LastPlayedDate")
            if user_ts and (latest_ts is None or user_ts > latest_ts):
                latest_ts = user_ts
            watch_events.append({"account_id": aid, "found_by": found_by, "updated_at": user_ts})
        out.update({"watch_events": watch_events, "all_watched": all_watched, "latest_watch_ts": latest_ts})
        if not all_watched:
            out.update({"verdict": "NOT_ALL_WATCHED", "verdict_detail": "At least one assigned user has not watched this item yet."})
            return out
        if latest_ts is None:
            out.update({"verdict": "NO_WATCH_EVENTS", "verdict_detail": "No watch events found in the database for this item."})
            return out
        diag_movie = rating_key if item_type == "movie" else None
        enabled_since = adc.get_enabled_since(db, cfg, library_ns, series_ns, diag_movie)
        out["enabled_since"] = enabled_since.isoformat() if enabled_since else None
        try:
            ts_dt = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
        except Exception:
            out.update({"verdict": "TIMESTAMP_PARSE_ERROR", "verdict_detail": f"Could not parse latest watch timestamp: {latest_ts}"})
            return out
        if not (enabled_since is None or ts_dt > enabled_since):
            out.update({"timestamp_gate_passed": False, "verdict": "PRE_OPT_IN",
                        "verdict_detail": f"All users watched, but the last watch ({latest_ts}) predates opt-in ({enabled_since.isoformat()})."})
            return out
        out["timestamp_gate_passed"] = True
        now = datetime.now(timezone.utc)
        elapsed = now - ts_dt
        eh = elapsed.total_seconds() / 3600
        em = elapsed.total_seconds() / 60
        grace_passed = grace_days == 0 or elapsed >= timedelta(days=grace_days)
        min_delay_passed = min_delay == 0 or elapsed >= timedelta(minutes=min_delay)
        out.update({"grace_elapsed_hours": round(eh, 2), "grace_required_hours": grace_days * 24, "grace_passed": grace_passed,
                    "min_delay_elapsed_minutes": round(em, 1), "min_delay_required_minutes": min_delay, "min_delay_passed": min_delay_passed})
        if not grace_passed:
            out.update({"verdict": "WAITING_FOR_GRACE", "verdict_detail": f"All users watched. Waiting for {grace_days}-day grace (~{round(grace_days * 24 - eh, 1)}h left)."})
            return out
        if not min_delay_passed:
            out.update({"verdict": "WAITING_FOR_MIN_DELAY", "verdict_detail": f"All users watched. Waiting for min delay ({min_delay} min, ~{round(min_delay - em, 1)} left)."})
            return out
        out.update({"verdict": "WOULD_DELETE", "verdict_detail": "All conditions pass. Would be deleted on the next sweep tick."})
        return out
    finally:
        db.close()


# ── recently-added discovery ──────────────────────────────────────────
def _norm_created(s):
    """Normalise a Jellyfin DateCreated string to canonical UTC ISO (+00:00)."""
    if not s:
        return None
    try:
        t = s.replace("Z", "+00:00")
        t = re.sub(r"(\.\d{6})\d+", r"\1", t)  # trim >6-digit fractions to 6
        return datetime.fromisoformat(t).astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def recently_added_scan(cfg):
    show_all = cfg.get("show_all_libraries", True)
    monitored = set(cfg.get("monitored_libraries", []))
    out = []
    try:
        sections = jellyfin_libraries()
    except Exception:
        return out
    for s in sections:
        ctype = (s.get("CollectionType") or "").lower()
        if ctype not in ("tvshows", "movies"):
            continue
        lib_ns = _ns(str(s["Id"]))
        if not (show_all or lib_ns in monitored):
            continue
        jf_type = "Movie" if ctype == "movies" else "Series"
        item_type = "movie" if ctype == "movies" else "series"
        try:
            data = jellyfin_get("/Items", {
                "ParentId": str(s["Id"]), "IncludeItemTypes": jf_type, "Recursive": "true",
                "SortBy": "DateCreated", "SortOrder": "Descending",
                "fields": "ProviderIds,DateCreated", "Limit": 200, "StartIndex": 0})
        except Exception:
            continue
        for i in data.get("Items", []):
            rk = str(i.get("Id", ""))
            if not rk:
                continue
            providers = parse_jellyfin_providers(i.get("ProviderIds") or {})
            first_seen = _norm_created(i.get("DateCreated")) or datetime.now(timezone.utc).isoformat()
            out.append({"providers": providers, "item_type": item_type, "library_id": lib_ns,
                        "rating_key": _ns(rk), "title": i.get("Name", ""),
                        "year": i.get("ProductionYear"), "first_seen": first_seen})
    return out


# Register the item->providers resolver for the shared modules.
media_lookup.register(KEY, get_item_providers)
