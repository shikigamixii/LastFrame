"""Plex provider engine.

Wraps plex_api into the uniform engine interface app.py consumes. Every id
returned to the app/frontend is namespaced ('px_<ratingKey>'); ids are stripped
back to raw only when talking to Plex's HTTP API. The server owner's viewCount /
viewedLeafCount is used as a reliable per-owner fallback where Plex provides no
webhook event, mirroring the original Plex-only build.
"""
import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta

import idutil
import media_lookup
from flask import Response

from config_store import load_config, PAGE_SIZE
from db import get_db
from plex_api import (
    get_plex_url, get_plex_token, get_webhook_secret,
    plex_get, plex_delete, plex_get_raw, plex_get_with_token,
    plex_accounts, plex_all_accounts, plex_owner_id, _local_account_id,
    plex_sections, plex_genre_id, plex_tv_home_users, plex_tv_switch_token,
    plex_tv_switch_token_diag, plex_refresh_access_tokens,
    ts_to_iso, parse_plex_guids, get_item_providers as _raw_item_providers,
)
from assignments import get_assigned_ids, _load_assignment_maps, _resolve_target
from webhook_state import (
    pwe_has_data, pwe_get_played, pwe_get_played_with_ts, pwe_get_played_by_ratingkey,
    is_item_watched_pwe, get_last_played_pwe,
)
import auto_delete_core as adc

logger = logging.getLogger(__name__)

KEY = "plex"
LABEL = "Plex"
WEBHOOK_PATH = "/api/webhook/plex"

_RAW_ID_RE = re.compile(r'^\d{1,20}$')


def _ns(raw):
    return idutil.make_id(KEY, raw)


def valid_raw(raw):
    return bool(raw and _RAW_ID_RE.match(str(raw)))


# ── identity / config ─────────────────────────────────────────────────
def is_configured():
    return bool(get_plex_token())


def is_enabled(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    return bool(cfg.get("plex_enabled")) and is_configured()


def webhook_secret():
    return get_webhook_secret()


def get_item_providers(raw):
    return _raw_item_providers(raw)


# ── accounts ──────────────────────────────────────────────────────────
def _accounts_full():
    return [{"id": _ns(a["id"]), "raw": a["id"], "name": a["name"]} for a in plex_accounts()]


def accounts():
    return [{"id": a["id"], "name": a["name"]} for a in _accounts_full()]


def all_accounts():
    cfg = load_config()
    hidden = set(cfg.get("hidden_accounts", []))
    out = []
    for a in plex_all_accounts():
        nsid = _ns(a["id"])
        out.append({"id": nsid, "name": a["name"] or f"(unnamed #{a['id']})", "hidden": nsid in hidden})
    return out


def owner_id():
    oid = plex_owner_id()
    return _ns(oid) if oid else None


# ── libraries / browse ────────────────────────────────────────────────
def libraries(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    out = []
    for s in plex_sections():
        if s.get("type") not in ("show", "movie"):
            continue
        nsid = _ns(str(s["key"]))
        ltype = "tvshows" if s["type"] == "show" else "movies"
        out.append({"id": nsid, "name": s["title"], "type": ltype,
                    "monitored": cfg.get("show_all_libraries", True) or nsid in cfg.get("monitored_libraries", [])})
    return out


def titles_by_ids(kind, raw_ids):
    items = []
    for rid in raw_ids:
        if not valid_raw(rid):
            continue
        try:
            mc = plex_get(f"/library/metadata/{rid}")
            m = (mc.get("Metadata") or [])[0]
            items.append({"id": _ns(str(m["ratingKey"])), "name": m.get("title", ""), "year": m.get("year")})
        except Exception:
            continue
    return items


def list_titles(kind, parent_raw, page, search, genre):
    plex_type = 1 if kind == "movies" else 2
    params = {"type": plex_type, "sort": "titleSort:asc",
              "X-Plex-Container-Size": 100 if search else PAGE_SIZE,
              "X-Plex-Container-Start": 0 if search else (page - 1) * PAGE_SIZE}
    if search:
        params["title"] = search
    if genre:
        gid = plex_genre_id(parent_raw, genre)
        if gid:
            params["genre"] = gid
    mc = plex_get(f"/library/sections/{parent_raw}/all", params)
    items = mc.get("Metadata", [])
    total = mc.get("totalSize", mc.get("size", len(items)))
    return {"items": [{"id": _ns(str(i["ratingKey"])), "name": i.get("title", ""), "year": i.get("year")} for i in items],
            "totalCount": total}


def genres(parent_raw):
    mc = plex_get(f"/library/sections/{parent_raw}/genre")
    return sorted(set((g.get("title") or g.get("tag") or "")
                      for g in mc.get("Directory", []) if (g.get("title") or g.get("tag"))))


def search(cfg):
    show_all = cfg.get("show_all_libraries", True)
    monitored = set(cfg.get("monitored_libraries", []))
    q = cfg.get("_q", "")
    results = []
    for s in plex_sections():
        stype = s.get("type")
        if stype not in ("show", "movie"):
            continue
        nsid = _ns(str(s["key"]))
        if not show_all and nsid not in monitored:
            continue
        plex_type = 1 if stype == "movie" else 2
        kind = "movie" if stype == "movie" else "series"
        try:
            mc = plex_get(f"/library/sections/{s['key']}/all", {
                "type": plex_type, "title": q, "sort": "titleSort:asc",
                "X-Plex-Container-Size": 25, "X-Plex-Container-Start": 0})
        except Exception:
            continue
        for i in mc.get("Metadata", []):
            results.append({"id": _ns(str(i["ratingKey"])), "name": i.get("title", ""),
                            "year": i.get("year"), "type": kind, "libId": nsid})
    return results


# ── item info ─────────────────────────────────────────────────────────
def item_info(raw):
    mc = plex_get(f"/library/metadata/{raw}")
    items = mc.get("Metadata") or mc.get("Directory") or []
    if not items:
        return None
    i = items[0]
    plex_type = i.get("type", "")
    type_map = {"show": ("tvshows", "Series"), "movie": ("movies", "Movie"),
                "season": ("tvshows", "Season"), "episode": ("tvshows", "Episode")}
    ctype, itype = type_map.get(plex_type, ("", plex_type))
    return {
        "id": _ns(str(i.get("ratingKey", raw))), "name": i.get("title", ""),
        "type": itype, "collectionType": ctype,
        "seriesId": _ns(str(i["grandparentRatingKey"])) if i.get("grandparentRatingKey") else None,
        "seriesName": i.get("grandparentTitle") or None,
    }


# ── watch summary ─────────────────────────────────────────────────────
def watch_summary(parent_raw, kind):
    accts = _accounts_full()
    if not accts:
        return {}
    all_account_ids = {a["id"] for a in accts}
    plex_type = 1 if kind == "movies" else 2

    mc = plex_get(f"/library/sections/{parent_raw}/all",
                  {"type": plex_type, "X-Plex-Container-Size": 10000, "X-Plex-Container-Start": 0, "includeGuids": 1})
    all_items = mc.get("Metadata", [])
    if not all_items:
        return {}

    db = get_db()
    item_assignments, provider_assignments = _load_assignment_maps(db)

    def resolve_target(providers, item_id):
        return _resolve_target(providers, item_id, item_assignments, provider_assignments, all_account_ids)

    has_pwe = pwe_has_data(db)
    own = owner_id()
    result = {}
    if has_pwe:
        if kind == "movies":
            played = pwe_get_played(db, list(all_account_ids), ["movie"])
            played_rk = pwe_get_played_by_ratingkey(db, list(all_account_ids), ["movie"])
            for i in all_items:
                iid = _ns(str(i["ratingKey"]))
                providers = parse_plex_guids(i.get("Guid", []))
                target_ids = resolve_target(providers, iid)
                owner_saw_it = (i.get("viewCount") or 0) > 0
                if not target_ids:
                    result[iid] = {"watched": 0, "total": 0, "watcher_ids": []}
                    continue
                watched_count = 0
                watcher_ids = []
                rk_watchers = played_rk.get(iid, set())
                for aid in target_ids:
                    saw_it = (aid == own and owner_saw_it) or (
                        bool(providers) and is_item_watched_pwe(providers, "movie", {aid}, played)) or (aid in rk_watchers)
                    if saw_it:
                        watched_count += 1
                        watcher_ids.append(aid)
                result[iid] = {"watched": watched_count, "total": len(target_ids), "watcher_ids": watcher_ids}
        else:
            ep_mc = plex_get(f"/library/sections/{parent_raw}/all",
                             {"type": 4, "X-Plex-Container-Size": 50000, "X-Plex-Container-Start": 0, "includeGuids": 1})
            show_eps = {}
            for ep in ep_mc.get("Metadata", []):
                show_id = _ns(str(ep.get("grandparentRatingKey", ""))) if ep.get("grandparentRatingKey") else ""
                ep_rk = _ns(str(ep.get("ratingKey", ""))) if ep.get("ratingKey") else ""
                providers = parse_plex_guids(ep.get("Guid", []))
                if show_id and (providers or ep_rk):
                    show_eps.setdefault(show_id, []).append((providers, ep_rk))
            ep_played = pwe_get_played(db, list(all_account_ids), ["episode"])
            ep_played_rk = pwe_get_played_by_ratingkey(db, list(all_account_ids), ["episode"])
            for i in all_items:
                iid = _ns(str(i["ratingKey"]))
                show_providers = parse_plex_guids(i.get("Guid", []))
                target_ids = resolve_target(show_providers, iid)
                leaf = i.get("leafCount") or 0
                viewed = i.get("viewedLeafCount") or 0
                owner_saw_all = leaf > 0 and viewed >= leaf
                eps = show_eps.get(iid, [])
                if not target_ids or not eps:
                    result[iid] = {"watched": 0, "total": len(target_ids), "watcher_ids": []}
                    continue
                watched_count = 0
                watcher_ids = []
                for aid in target_ids:
                    if aid == own and owner_saw_all:
                        saw_all = True
                    else:
                        saw_all = all(
                            (bool(p) and is_item_watched_pwe(p, "episode", {aid}, ep_played)) or
                            (rk and aid in ep_played_rk.get(rk, set()))
                            for p, rk in eps)
                    if saw_all:
                        watched_count += 1
                        watcher_ids.append(aid)
                result[iid] = {"watched": watched_count, "total": len(target_ids), "watcher_ids": watcher_ids}
    else:
        owner_watched = set()
        for i in all_items:
            rid = _ns(str(i["ratingKey"]))
            if kind == "movies":
                if (i.get("viewCount") or 0) > 0:
                    owner_watched.add(rid)
            else:
                leaf = i.get("leafCount") or 0
                viewed = i.get("viewedLeafCount") or 0
                if leaf > 0 and viewed >= leaf:
                    owner_watched.add(rid)
        for i in all_items:
            iid = _ns(str(i["ratingKey"]))
            item_providers = parse_plex_guids(i.get("Guid", []))
            target_ids = resolve_target(item_providers, iid)
            owner_watched_it = iid in owner_watched and own in target_ids
            wids = [own] if owner_watched_it else []
            result[iid] = {"watched": len(wids), "total": len(target_ids), "watcher_ids": wids}
    db.close()
    return result


def _bulk_metadata_by_keys(raw_keys, include_guids=False):
    keys = [str(k) for k in raw_keys if k]
    if not keys:
        return {}
    out = {}
    params = {"includeGuids": 1} if include_guids else None
    for i in range(0, len(keys), 100):
        chunk = keys[i:i + 100]
        try:
            mc = plex_get(f"/library/metadata/{','.join(chunk)}", params)
        except Exception:
            continue
        for it in (mc.get("Metadata") or []):
            rk = str(it.get("ratingKey") or "")
            if rk:
                out[rk] = it
    return out


def watch_summary_items(movie_raws, ep_raws):
    accts = _accounts_full()
    if not accts:
        return {}
    all_account_ids = {a["id"] for a in accts}
    own = owner_id()

    db = get_db()
    item_assignments, provider_assignments = _load_assignment_maps(db)
    has_pwe = pwe_has_data(db)

    def resolve_target(providers, item_id):
        return _resolve_target(providers, item_id, item_assignments, provider_assignments, all_account_ids)

    played = pwe_get_played(db, list(all_account_ids), ["movie", "episode"]) if has_pwe else {}
    db.close()

    items_by_id = _bulk_metadata_by_keys({*movie_raws, *ep_raws}, include_guids=True)
    grandparent_ids = {str(items_by_id.get(eid, {}).get("grandparentRatingKey") or "") for eid in ep_raws}
    grandparent_ids.discard("")
    show_meta = _bulk_metadata_by_keys(grandparent_ids, include_guids=True) if grandparent_ids else {}
    show_providers_by_id = {gid: parse_plex_guids(it.get("Guid", [])) for gid, it in show_meta.items()}

    result = {}
    for raw in movie_raws:
        item = items_by_id.get(raw)
        iid = _ns(raw)
        if not item:
            result[iid] = {"watched": 0, "total": 0, "watcher_ids": []}
            continue
        providers = parse_plex_guids(item.get("Guid", []))
        target_ids = resolve_target(providers, iid)
        owner_view_count = item.get("viewCount") or 0
        if has_pwe:
            watched_count = 0
            watcher_ids = []
            for aid in target_ids:
                saw_it = (aid == own and owner_view_count > 0) or (
                    bool(providers) and is_item_watched_pwe(providers, "movie", {aid}, played))
                if saw_it:
                    watched_count += 1
                    watcher_ids.append(aid)
            result[iid] = {"watched": watched_count, "total": len(target_ids), "watcher_ids": watcher_ids}
        else:
            owner_saw_it = owner_view_count > 0 and own in target_ids
            wids = [own] if owner_saw_it else []
            result[iid] = {"watched": len(wids), "total": len(target_ids), "watcher_ids": wids}

    for raw in ep_raws:
        item = items_by_id.get(raw)
        iid = _ns(raw)
        if not item:
            result[iid] = {"watched": 0, "total": 0, "watcher_ids": []}
            continue
        providers = parse_plex_guids(item.get("Guid", []))
        grandparent_raw = str(item.get("grandparentRatingKey", ""))
        show_providers = show_providers_by_id.get(grandparent_raw, {}) if grandparent_raw else {}
        target_ids = resolve_target(show_providers, _ns(grandparent_raw) if grandparent_raw else "")
        owner_view_count = item.get("viewCount") or 0
        if has_pwe:
            watched_count = 0
            watcher_ids = []
            for aid in target_ids:
                saw_it = is_item_watched_pwe(providers, "episode", {aid}, played) if providers else False
                if not saw_it and aid == own and owner_view_count > 0:
                    saw_it = True
                if saw_it:
                    watched_count += 1
                    watcher_ids.append(aid)
            result[iid] = {"watched": watched_count, "total": len(target_ids), "watcher_ids": watcher_ids}
        else:
            owner_saw_it = owner_view_count > 0 and own in target_ids
            wids = [own] if owner_saw_it else []
            result[iid] = {"watched": len(wids), "total": len(target_ids), "watcher_ids": wids}
    return result


# ── seasons / episodes / watch status ─────────────────────────────────
def seasons(series_raw):
    mc = plex_get(f"/library/metadata/{series_raw}/children")
    seasons_raw = [s for s in mc.get("Metadata", []) if s.get("type") == "season"]
    if not seasons_raw:
        return []
    accts = _accounts_full()
    assigned_ids = get_assigned_ids(_ns(series_raw))
    target_accounts = accts if assigned_ids is None else [a for a in accts if a["id"] in assigned_ids]
    if not target_accounts:
        target_accounts = accts

    db = get_db()
    has_pwe = pwe_has_data(db)
    own = owner_id()
    if has_pwe:
        target_ids = [a["id"] for a in target_accounts]
        played = pwe_get_played(db, target_ids, ["episode"])
        db.close()
        try:
            leaf_mc = plex_get(f"/library/metadata/{series_raw}/allLeaves", {"includeGuids": 1})
            all_eps = leaf_mc.get("Metadata", [])
        except Exception:
            all_eps = []
        season_eps = {}
        for ep in all_eps:
            sk = str(ep.get("parentRatingKey", ""))
            if sk:
                season_eps.setdefault(sk, []).append((_ns(str(ep["ratingKey"])), parse_plex_guids(ep.get("Guid", []))))
        result = []
        for s in seasons_raw:
            sid_raw = str(s["ratingKey"])
            base_total = s.get("leafCount") or 0
            owner_viewed = s.get("viewedLeafCount") or 0
            eps = season_eps.get(sid_raw, [])
            completed_users, per_user = 0, []
            for acc in target_accounts:
                if acc["id"] == own and base_total > 0 and owner_viewed >= base_total:
                    played_count, total, completed = owner_viewed, base_total, True
                elif eps:
                    total = len(eps)
                    played_count = sum(1 for _, providers in eps
                                       if providers and is_item_watched_pwe(providers, "episode", {acc["id"]}, played))
                    completed = total > 0 and played_count >= total
                else:
                    played_count, total, completed = 0, base_total, False
                if completed:
                    completed_users += 1
                per_user.append({"userId": acc["id"], "userName": acc["name"],
                                 "playedCount": played_count, "totalCount": total, "completed": completed})
            result.append({"id": _ns(sid_raw), "name": s.get("title", ""), "indexNumber": s.get("index", 0),
                           "totalEpisodes": base_total, "userProgress": per_user,
                           "completedUsers": completed_users, "totalAssignedUsers": len(target_accounts)})
        return result

    db.close()
    result = []
    for s in seasons_raw:
        sid_raw = str(s["ratingKey"])
        base_total = s.get("leafCount") or 0
        owner_played = s.get("viewedLeafCount") or 0
        completed_users, per_user = 0, []
        for acc in target_accounts:
            if acc["id"] == own:
                played, total = owner_played, base_total
            else:
                played, total = 0, base_total
            completed = total > 0 and played >= total
            if completed:
                completed_users += 1
            per_user.append({"userId": acc["id"], "userName": acc["name"],
                             "playedCount": played, "totalCount": total, "completed": completed})
        result.append({"id": _ns(sid_raw), "name": s.get("title", ""), "indexNumber": s.get("index", 0),
                       "totalEpisodes": base_total, "userProgress": per_user,
                       "completedUsers": completed_users, "totalAssignedUsers": len(target_accounts)})
    return result


def season_watch_status(series_raw, season_raw):
    accts = _accounts_full()
    db = get_db()
    has_pwe = pwe_has_data(db)
    own = owner_id()
    if has_pwe:
        all_ids = [a["id"] for a in accts]
        played = pwe_get_played(db, all_ids, ["episode"])
        played_ts = pwe_get_played_with_ts(db, all_ids, ["episode"])
        db.close()
        mc = plex_get(f"/library/metadata/{season_raw}/children", {"includeGuids": 1})
        episodes = [ep for ep in mc.get("Metadata", []) if ep.get("type") == "episode"]
        result = []
        for ep in episodes:
            eid = _ns(str(ep["ratingKey"]))
            providers = parse_plex_guids(ep.get("Guid", []))
            owner_view_count = ep.get("viewCount") or 0
            users = []
            for acc in accts:
                if providers:
                    is_played = is_item_watched_pwe(providers, "episode", {acc["id"]}, played)
                    if not is_played and acc["id"] == own and owner_view_count > 0:
                        is_played = True
                    last_ts = get_last_played_pwe(providers, "episode", acc["id"], played_ts)
                    if not last_ts and acc["id"] == own and owner_view_count > 0:
                        last_ts = ts_to_iso(ep.get("lastViewedAt"))
                else:
                    is_played = acc["id"] == own and owner_view_count > 0
                    last_ts = ts_to_iso(ep.get("lastViewedAt")) if is_played else None
                users.append({"userId": acc["id"], "userName": acc["name"], "played": is_played,
                              "playCount": 1 if is_played else 0, "lastPlayedDate": last_ts,
                              "playedPercentage": 100.0 if is_played else 0.0})
            result.append({"id": eid, "name": ep.get("title", ""), "indexNumber": ep.get("index", 0),
                           "runTimeTicks": (ep.get("duration") or 0) * 10000, "users": users})
        return result

    db.close()
    mc = plex_get(f"/library/metadata/{season_raw}/children")
    episodes = [ep for ep in mc.get("Metadata", []) if ep.get("type") == "episode"]
    result = []
    for ep in episodes:
        eid = _ns(str(ep["ratingKey"]))
        view_count = ep.get("viewCount") or 0
        view_offset = ep.get("viewOffset") or 0
        duration_ms = ep.get("duration") or 1
        owner_played = view_count > 0
        owner_pct = 100.0 if owner_played else round(min(view_offset / duration_ms * 100, 99.9), 1)
        users = []
        for acc in accts:
            if acc["id"] == own:
                users.append({"userId": acc["id"], "userName": acc["name"], "played": owner_played,
                              "playCount": view_count, "lastPlayedDate": ts_to_iso(ep.get("lastViewedAt")),
                              "playedPercentage": owner_pct})
            else:
                users.append({"userId": acc["id"], "userName": acc["name"], "played": False,
                              "playCount": 0, "lastPlayedDate": None, "playedPercentage": 0})
        result.append({"id": eid, "name": ep.get("title", ""), "indexNumber": ep.get("index", 0),
                       "runTimeTicks": (ep.get("duration") or 0) * 10000, "users": users})
    return sorted(result, key=lambda x: x["indexNumber"])


def watch_status(raw):
    accts = _accounts_full()
    db = get_db()
    has_pwe = pwe_has_data(db)
    own = owner_id()
    if has_pwe:
        mc = plex_get(f"/library/metadata/{raw}", {"includeGuids": 1})
        item = (mc.get("Metadata") or [{}])[0]
        providers = parse_plex_guids(item.get("Guid", []))
        raw_type = item.get("type", "movie")
        all_ids = [a["id"] for a in accts]
        if raw_type == "show":
            try:
                leaf_mc = plex_get(f"/library/metadata/{raw}/allLeaves", {"includeGuids": 1})
                eps = [parse_plex_guids(e.get("Guid", [])) for e in leaf_mc.get("Metadata", [])]
                eps = [p for p in eps if p]
            except Exception:
                eps = []
            ep_played = pwe_get_played(db, all_ids, ["episode"])
            ep_played_ts = pwe_get_played_with_ts(db, all_ids, ["episode"])
            db.close()
            leaf = item.get("leafCount") or 0
            viewed = item.get("viewedLeafCount") or 0
            owner_saw_all = leaf > 0 and viewed >= leaf
            out = []
            for acc in accts:
                if acc["id"] == own and owner_saw_all:
                    is_played = True
                elif eps:
                    is_played = all(is_item_watched_pwe(p, "episode", {acc["id"]}, ep_played) for p in eps)
                else:
                    is_played = False
                last_ts = None
                if is_played and eps:
                    for p in eps:
                        t = get_last_played_pwe(p, "episode", acc["id"], ep_played_ts)
                        if t and (last_ts is None or t > last_ts):
                            last_ts = t
                out.append({"userId": acc["id"], "userName": acc["name"], "played": is_played,
                            "playCount": 1 if is_played else 0, "lastPlayedDate": last_ts,
                            "playedPercentage": 100.0 if is_played else 0.0})
        else:
            ws_itype = {"movie": "movie", "episode": "episode"}.get(raw_type, "movie")
            played = pwe_get_played(db, all_ids, [ws_itype])
            played_ts = pwe_get_played_with_ts(db, all_ids, [ws_itype])
            db.close()
            owner_saw_it = (item.get("viewCount") or 0) > 0
            out = []
            for acc in accts:
                if acc["id"] == own and owner_saw_it:
                    is_played = True
                    last_ts = ts_to_iso(item.get("lastViewedAt"))
                elif providers:
                    is_played = is_item_watched_pwe(providers, ws_itype, {acc["id"]}, played)
                    last_ts = get_last_played_pwe(providers, ws_itype, acc["id"], played_ts)
                else:
                    is_played = False
                    last_ts = None
                out.append({"userId": acc["id"], "userName": acc["name"], "played": is_played,
                            "playCount": 1 if is_played else 0, "lastPlayedDate": last_ts,
                            "playedPercentage": 100.0 if is_played else 0.0})
        return out

    db.close()
    out = []
    try:
        mc = plex_get(f"/library/metadata/{raw}")
        i = (mc.get("Metadata") or [])[0]
        view_count = i.get("viewCount") or 0
        view_offset = i.get("viewOffset") or 0
        duration_ms = i.get("duration") or 1
        owner_played = view_count > 0
        owner_pct = 100.0 if owner_played else round(min(view_offset / duration_ms * 100, 99.9), 1)
        owner_date = ts_to_iso(i.get("lastViewedAt"))
    except Exception:
        owner_played, owner_pct, owner_date, view_count = False, 0, None, 0
    for acc in accts:
        if acc["id"] == own:
            out.append({"userId": acc["id"], "userName": acc["name"], "played": owner_played,
                        "playCount": view_count, "lastPlayedDate": owner_date, "playedPercentage": owner_pct})
        else:
            out.append({"userId": acc["id"], "userName": acc["name"], "played": False,
                        "playCount": 0, "lastPlayedDate": None, "playedPercentage": 0})
    return out


# ── recent / activity ─────────────────────────────────────────────────
def _monitored(cfg):
    return set(cfg.get("monitored_libraries", []))


def recent(kind, cfg):
    monitored = _monitored(cfg)
    show_all = cfg.get("show_all_libraries", True)
    stype = "movie" if kind == "movies" else "show"
    plex_type = 1 if kind == "movies" else 4
    db_item_type = "movie" if kind == "movies" else "episode"
    sections = [s for s in plex_sections()
                if s.get("type") == stype and (show_all or _ns(str(s["key"])) in monitored)]
    section_ids = {str(s["key"]) for s in sections}
    result = {}

    for sec in sections:
        try:
            mc = plex_get(f"/library/sections/{sec['key']}/all",
                          {"type": plex_type, "sort": "lastViewedAt:desc", "X-Plex-Container-Size": 100})
            for i in mc.get("Metadata", []):
                lv = i.get("lastViewedAt")
                if not lv or not (i.get("viewCount") or 0):
                    continue
                nsid = _ns(str(i["ratingKey"]))
                if kind == "movies":
                    result[nsid] = {"id": nsid, "name": i.get("title", ""), "year": i.get("year"),
                                    "imageUrl": f"/api/image/{nsid}?type=Primary&maxWidth=200", "lastPlayedDate": ts_to_iso(lv)}
                else:
                    series_id = str(i.get("grandparentRatingKey", ""))
                    img = _ns(series_id) if series_id else nsid
                    result[nsid] = {"id": nsid, "name": i.get("title", ""), "seriesName": i.get("grandparentTitle", ""),
                                    "seasonName": i.get("parentTitle", ""), "episodeNumber": i.get("index"),
                                    "imageUrl": f"/api/image/{img}?type=Primary&maxWidth=200",
                                    "lastPlayedDate": ts_to_iso(lv), "seriesId": _ns(series_id) if series_id else ""}
        except Exception:
            continue

    db = get_db()
    rows = db.execute(
        "SELECT rating_key, MAX(updated_at) as latest_at FROM watch_events "
        "WHERE event_type='play' AND item_type=? AND rating_key != '' "
        "GROUP BY rating_key ORDER BY latest_at DESC LIMIT 200", (db_item_type,)).fetchall()
    db.close()

    ts_by_ns = {}
    for r in rows:
        k, raw = idutil.split_id(r["rating_key"])
        if k != KEY:
            continue
        nsid = r["rating_key"]
        if (result.get(nsid, {}).get("lastPlayedDate") or "") < r["latest_at"]:
            ts_by_ns[nsid] = (raw, r["latest_at"])

    items = _bulk_metadata_by_keys([v[0] for v in ts_by_ns.values()])
    for nsid, (raw, ts) in ts_by_ns.items():
        i = items.get(raw)
        if not i or str(i.get("librarySectionID", "")) not in section_ids:
            continue
        if kind == "movies":
            result[nsid] = {"id": nsid, "name": i.get("title", ""), "year": i.get("year"),
                            "imageUrl": f"/api/image/{nsid}?type=Primary&maxWidth=200", "lastPlayedDate": ts}
        else:
            series_id = str(i.get("grandparentRatingKey", ""))
            img = _ns(series_id) if series_id else nsid
            result[nsid] = {"id": nsid, "name": i.get("title", ""), "seriesName": i.get("grandparentTitle", ""),
                            "seasonName": i.get("parentTitle", ""), "episodeNumber": i.get("index"),
                            "imageUrl": f"/api/image/{img}?type=Primary&maxWidth=200",
                            "lastPlayedDate": ts, "seriesId": _ns(series_id) if series_id else ""}
    return list(result.values())


def activity():
    try:
        mc = plex_get("/status/sessions")
    except Exception:
        return []
    sessions = mc.get("Metadata") or mc.get("Video") or mc.get("Track") or []
    out = []
    for i in sessions:
        rid = str(i.get("ratingKey", ""))
        view_offset = i.get("viewOffset") or 0
        duration = i.get("duration") or 1
        progress = round(min(view_offset / duration * 100, 100), 1)
        media = i.get("Media") or []
        part_decision = ""
        if media and media[0].get("Part"):
            part_decision = (media[0]["Part"][0].get("decision") or "").lower()
        stream_type = "Direct Play" if part_decision == "directplay" else ("Transcode" if part_decision == "transcode" else "")
        bandwidth_kbps = (i.get("Session") or {}).get("bandwidth") or 0
        bandwidth_mbps = round(bandwidth_kbps / 1000, 1) if bandwidth_kbps else None
        gp = str(i.get("grandparentRatingKey") or "")
        out.append({
            "id": _ns(str((i.get("Session") or {}).get("id") or rid)), "type": i.get("type", ""), "provider": KEY,
            "title": i.get("title", ""), "seriesName": i.get("grandparentTitle") or None,
            "seasonName": i.get("parentTitle") or None, "episodeNumber": i.get("index"),
            "imageUrl": f"/api/image/{_ns(rid)}?type=Primary&maxWidth=300" if rid else None,
            "progress": progress, "viewOffset": view_offset, "duration": duration,
            "user": (i.get("User") or {}).get("title", "Unknown"), "player": (i.get("Player") or {}).get("title", ""),
            "streamType": stream_type, "bandwidth": bandwidth_mbps,
            "ratingKey": _ns(rid) if rid else "",
            "grandparentRatingKey": _ns(gp) if gp else "",
            "parentRatingKey": _ns(str(i.get("parentRatingKey"))) if i.get("parentRatingKey") else "",
            "librarySectionID": _ns(str(i.get("librarySectionID"))) if i.get("librarySectionID") else "",
        })
    return out


# ── history import / backfill ─────────────────────────────────────────
def import_history(max_pages=None):
    """Pull play events from Plex's session history into watch_events."""
    accts = plex_accounts()
    if not accts:
        return {"ok": False, "error": "no accounts", "imported": 0, "total_history": 0}
    valid_ids = {a["id"] for a in accts}
    batch = 500
    start = 0
    pages = 0
    all_history = []
    while True:
        try:
            mc = plex_get("/status/sessions/history/all",
                          {"sort": "viewedAt:desc", "X-Plex-Container-Start": start, "X-Plex-Container-Size": batch})
        except Exception:
            break
        items = mc.get("Metadata") or mc.get("Video") or []
        all_history.extend(items)
        total = int(mc.get("totalSize") or mc.get("size") or 0)
        start += len(items)
        pages += 1
        if not items or start >= total:
            break
        if max_pages is not None and pages >= max_pages:
            break

    target_types = {"movie", "episode"}
    filtered = [i for i in all_history
                if (i.get("type") or "").lower() in target_types
                and str(i.get("accountID") or "") in valid_ids
                and i.get("ratingKey") and i.get("viewedAt")]
    unique_keys = list({str(i["ratingKey"]) for i in filtered})
    guid_map = {}
    for i in range(0, len(unique_keys), 100):
        chunk = unique_keys[i:i + 100]
        try:
            mc = plex_get(f"/library/metadata/{','.join(chunk)}", {"includeGuids": 1})
            for item in (mc.get("Metadata") or []):
                rk = str(item.get("ratingKey", ""))
                providers = parse_plex_guids(item.get("Guid", []))
                if rk and providers:
                    guid_map[rk] = providers
        except Exception:
            continue

    db = get_db()
    imported = 0
    try:
        for item in filtered:
            rk = str(item["ratingKey"])
            providers = guid_map.get(rk)
            if not providers:
                continue
            account_ns = _ns(str(item["accountID"]))
            item_type = (item.get("type") or "").lower()
            iso_ts = datetime.fromtimestamp(item["viewedAt"], tz=timezone.utc).isoformat()
            for ptype, pid in providers.items():
                cur = db.execute(
                    "INSERT OR IGNORE INTO watch_events "
                    "(account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key) "
                    "VALUES (?, ?, ?, ?, 'play', ?, ?)",
                    (account_ns, ptype.lower(), str(pid), item_type, iso_ts, _ns(rk)))
                imported += cur.rowcount
        db.commit()
    finally:
        db.close()
    return {"ok": True, "imported": imported, "total_history": len(filtered)}


def backfill():
    return import_history()


def debug_history(key, title_q):
    try:
        mc = plex_get("/status/sessions/history/all",
                      {"sort": "viewedAt:desc", "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 500})
    except Exception:
        return {"ok": False, "error": "Failed to fetch history from media server"}, 502
    items = mc.get("Metadata") or mc.get("Video") or []
    out = []
    for i in items:
        if key and key not in (str(i.get("ratingKey") or ""), str(i.get("parentRatingKey") or ""),
                               str(i.get("grandparentRatingKey") or "")):
            continue
        if title_q:
            blob = " ".join(str(i.get(k) or "") for k in ("title", "grandparentTitle", "parentTitle")).lower()
            if title_q not in blob:
                continue
        viewed_at = i.get("viewedAt")
        out.append({"ratingKey": i.get("ratingKey"), "parentRatingKey": i.get("parentRatingKey"),
                    "grandparentRatingKey": i.get("grandparentRatingKey"), "type": i.get("type"),
                    "title": i.get("title"), "grandparentTitle": i.get("grandparentTitle"),
                    "season": i.get("parentIndex"), "episode": i.get("index"), "accountID": i.get("accountID"),
                    "viewedAt": viewed_at,
                    "viewedAtISO": datetime.fromtimestamp(viewed_at, tz=timezone.utc).isoformat() if viewed_at else None})
    return {"ok": True, "total_in_page": len(items), "matches": len(out), "accounts": accounts(),
            "sample_raw": items[0] if items else None, "results": out}, 200


# ── managed-user sweep (Plex Home users) ──────────────────────────────
_managed_user_token_cache = {}
_MANAGED_TOKEN_TTL = 3600


def _get_managed_user_token(home_user_id):
    now = time.monotonic()
    cached = _managed_user_token_cache.get(home_user_id)
    if cached and now - cached[1] < _MANAGED_TOKEN_TTL:
        return cached[0]
    try:
        token = plex_tv_switch_token(home_user_id)
    except Exception as e:
        logger.warning(f"Switch-user token mint failed for home id {home_user_id}: {e}")
        return None
    if token:
        _managed_user_token_cache[home_user_id] = (token, now)
    return token


def run_managed_sweep():
    report = {"users": [], "imported": 0, "skipped_reason": None, "refresh_status": None}
    if not get_plex_token() or not get_plex_url():
        report["skipped_reason"] = "no plex token or url"
        return report
    report["refresh_status"] = plex_refresh_access_tokens()
    try:
        home_users = plex_tv_home_users()
    except Exception as e:
        report["skipped_reason"] = f"plex.tv home users fetch failed: {e}"
        return report
    if not home_users:
        report["skipped_reason"] = "no home users returned"
        return report
    try:
        local_accounts = plex_all_accounts()
    except Exception as e:
        report["skipped_reason"] = f"local accounts fetch failed: {e}"
        return report
    local_by_name = {a["name"].strip().lower(): a["id"] for a in local_accounts}
    try:
        sections = plex_sections()
    except Exception as e:
        report["skipped_reason"] = f"sections fetch failed: {e}"
        return report
    target_sections = [s for s in sections if s.get("type") in ("movie", "show")]
    if not target_sections:
        report["skipped_reason"] = "no movie/show sections"
        return report

    db = get_db()
    first = True
    try:
        for hu in home_users:
            entry = {"home_id": hu.get("id"), "title": hu.get("title"), "admin": hu.get("admin"),
                     "protected": hu.get("protected"), "local_account_id": None, "token_ok": False,
                     "sections": [], "imported": 0, "skipped_reason": None}
            if hu.get("admin"):
                entry["skipped_reason"] = "admin (owner already covered by webhook)"
                report["users"].append(entry)
                continue
            title = (hu.get("title") or "").strip()
            if not title:
                entry["skipped_reason"] = "no title"
                report["users"].append(entry)
                continue
            local_id = local_by_name.get(title.lower())
            entry["local_account_id"] = local_id
            if not local_id:
                entry["skipped_reason"] = "no matching local /accounts entry"
                report["users"].append(entry)
                continue
            if hu.get("protected"):
                entry["skipped_reason"] = "PIN-protected"
                report["users"].append(entry)
                continue
            if not first and hu.get("id") not in _managed_user_token_cache:
                time.sleep(1.5)
            first = False
            user_token = _get_managed_user_token(hu.get("id"))
            if not user_token:
                diag = plex_tv_switch_token_diag(hu.get("id"))
                entry["switch_diag"] = diag
                entry["skipped_reason"] = f"switch token mint failed (status={diag.get('status')}, endpoint={diag.get('endpoint')})"
                report["users"].append(entry)
                continue
            entry["token_ok"] = True
            for s in target_sections:
                section_key = s.get("key")
                section_type = s.get("type")
                item_type_code = "1" if section_type == "movie" else "4"
                item_type = "movie" if section_type == "movie" else "episode"
                sect_entry = {"section_key": section_key, "section_title": s.get("title"),
                              "watched_returned": 0, "imported": 0, "error": None}
                try:
                    mc = plex_get_with_token(f"/library/sections/{section_key}/all", user_token,
                                             {"type": item_type_code, "unwatched": 0, "includeGuids": 1,
                                              "X-Plex-Container-Size": 10000})
                except Exception:
                    logger.exception("Failed to import section %s", section_key)
                    sect_entry["error"] = "Failed to import section"
                    entry["sections"].append(sect_entry)
                    continue
                items = mc.get("Metadata") or []
                sect_entry["watched_returned"] = len(items)
                for item in items:
                    rk = str(item.get("ratingKey") or "")
                    if not rk or (item.get("viewCount") or 0) <= 0:
                        continue
                    providers = parse_plex_guids(item.get("Guid") or [])
                    if not providers:
                        continue
                    last_viewed = item.get("lastViewedAt")
                    if not last_viewed:
                        continue
                    iso_ts = datetime.fromtimestamp(last_viewed, tz=timezone.utc).isoformat()
                    for ptype, pid in providers.items():
                        cur = db.execute(
                            "INSERT OR IGNORE INTO watch_events "
                            "(account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key) "
                            "VALUES (?, ?, ?, ?, 'play', ?, ?)",
                            (_ns(local_id), ptype.lower(), str(pid), item_type, iso_ts, _ns(rk)))
                        sect_entry["imported"] += cur.rowcount
                        entry["imported"] += cur.rowcount
                        report["imported"] += cur.rowcount
                entry["sections"].append(sect_entry)
            report["users"].append(entry)
        db.commit()
    finally:
        db.close()
    return report


# ── webhook ───────────────────────────────────────────────────────────
def store_webhook(request, cfg):
    payload_str = request.form.get("payload")
    if payload_str:
        try:
            data = json.loads(payload_str)
        except Exception:
            return {"error": "invalid payload"}, 400
    else:
        data = request.get_json(silent=True) or {}

    raw_event = (data.get("event") or "").lower()
    if raw_event != "media.scrobble":
        return {"ok": True, "skipped": raw_event}, 200

    account = data.get("Account") or {}
    account_id = account.get("id")
    if account_id is None:
        return {"error": "no account"}, 400
    local_id = _local_account_id(str(account_id), account.get("title", ""))
    account_ns = _ns(local_id)

    meta = data.get("Metadata") or {}
    raw_type = (meta.get("type") or "").lower()
    type_map = {"movie": "movie", "episode": "episode", "show": "show"}
    item_type = type_map.get(raw_type)
    if not item_type:
        return {"ok": True, "skipped": f"type:{raw_type}"}, 200

    providers = parse_plex_guids(meta.get("Guid") or [])
    rk_raw = str(meta.get("ratingKey") or "")
    rk_ns = _ns(rk_raw) if rk_raw else ""
    if not providers and not rk_ns:
        return {"ok": True, "skipped": "no provider ids or rating_key"}, 200

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
    logger.info(f"Plex webhook: account={account_ns} ({account.get('title', '')}) type={item_type} providers={providers} rating_key={rk_raw}")
    if item_type in ("movie", "episode"):
        try:
            maybe_auto_delete(db, cfg, rk_raw, item_type, providers, meta)
        except Exception as e:
            logger.warning(f"Auto-delete check error for {rk_raw}: {e}")
    db.close()
    return {"ok": True, "stored": stored}, 200


# ── delete / image / misc ─────────────────────────────────────────────
def delete(raw):
    plex_delete(f"/library/metadata/{raw}")


def check_season_empty(season_raw):
    try:
        mc = plex_get(f"/library/metadata/{season_raw}/children")
        episodes = [i for i in mc.get("Metadata", []) if i.get("type") == "episode"]
        return len(episodes) == 0
    except Exception:
        return False


def image_response(raw, w_int, img_type):
    w = str(w_int)
    h = str(int(w_int * 1.5))
    try:
        mc = plex_get(f"/library/metadata/{raw}")
        m = (mc.get("Metadata") or [{}])[0]
        thumb = m.get("thumb") or m.get("parentThumb") or m.get("grandparentThumb")
        if not thumb:
            return Response(status=404)
        r = plex_get_raw("/photo/:/transcode",
                         {"width": w, "height": h, "minSize": 1, "upscale": 1, "url": thumb})
        return Response(r.iter_content(8192), content_type=r.headers.get("Content-Type", "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        return Response(status=404)


# ── auto-delete ───────────────────────────────────────────────────────
def _owner_state(item_meta):
    """(owner_saw, owner_ts) from a Plex item's viewCount/lastViewedAt."""
    saw = bool((item_meta or {}).get("viewCount") or 0)
    ts = ts_to_iso((item_meta or {}).get("lastViewedAt")) if saw else None
    return saw, ts


def maybe_auto_delete(db, cfg, rk_raw, item_type, providers, meta):
    if not cfg.get("auto_delete_enabled") or not rk_raw:
        return
    library_raw = str(meta.get("librarySectionID") or "")
    series_raw = str(meta.get("grandparentRatingKey") or "") if item_type == "episode" else ""
    item_meta = meta
    if not library_raw:
        try:
            mc = plex_get(f"/library/metadata/{rk_raw}")
            item_meta = (mc.get("Metadata") or [{}])[0]
            library_raw = str(item_meta.get("librarySectionID") or "")
            if item_type == "episode" and not series_raw:
                series_raw = str(item_meta.get("grandparentRatingKey") or "")
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
        title = str(meta.get("title") or rk_raw)
        plex_delete(f"/library/metadata/{rk_raw}")
        logger.info(f"Auto-deleted {item_type} {rk_raw} '{title}' — all assigned users watched")
    except Exception as e:
        logger.warning(f"Auto-delete failed for {rk_raw}: {e}")


def run_sweep(cfg):
    all_account_ids = {a["id"] for a in _accounts_full()}
    if not all_account_ids:
        return 0
    own = owner_id()
    db = get_db()
    deleted = 0
    try:
        item_assignments, provider_assignments = _load_assignment_maps(db)
        grace_days = int(cfg.get("auto_delete_grace_days") or 0)
        min_delay = int(cfg.get("auto_delete_min_delay_minutes") or 30)
        auto_libs = {idutil.raw_of(nsid) for nsid in (cfg.get("auto_delete_libraries") or []) if idutil.key_of(nsid) == KEY}
        force_series = [r["scope_id"] for r in db.execute(
            "SELECT scope_id FROM auto_delete_overrides WHERE scope='series' AND enabled=1").fetchall()
            if idutil.key_of(r["scope_id"]) == KEY]
        force_movies = [r["scope_id"] for r in db.execute(
            "SELECT scope_id FROM auto_delete_overrides WHERE scope='movie' AND enabled=1").fetchall()
            if idutil.key_of(r["scope_id"]) == KEY]
        sections = plex_sections()
        section_map = {str(s["key"]): s for s in sections}
        to_scan = set(auto_libs)
        for sid_ns in force_series:
            try:
                mc = plex_get(f"/library/metadata/{idutil.raw_of(sid_ns)}")
                lib_id = str((mc.get("Metadata") or [{}])[0].get("librarySectionID") or "")
                if lib_id:
                    to_scan.add(lib_id)
            except Exception:
                pass
        for mid_ns in force_movies:
            try:
                mc = plex_get(f"/library/metadata/{idutil.raw_of(mid_ns)}")
                lib_id = str((mc.get("Metadata") or [{}])[0].get("librarySectionID") or "")
                if lib_id:
                    to_scan.add(lib_id)
            except Exception:
                pass

        for lib_raw in to_scan:
            section = section_map.get(lib_raw)
            if not section:
                continue
            lib_ns = _ns(lib_raw)
            lib_type = section.get("type", "")
            try:
                if lib_type == "movie":
                    mc = plex_get(f"/library/sections/{lib_raw}/all",
                                  {"type": 1, "X-Plex-Container-Size": 10000, "includeGuids": 1})
                    for i in mc.get("Metadata", []):
                        rk_raw = str(i.get("ratingKey", ""))
                        if not rk_raw:
                            continue
                        rk_ns = _ns(rk_raw)
                        if not adc.is_auto_delete_active(db, cfg, lib_ns, movie_id=rk_ns):
                            continue
                        providers = parse_plex_guids(i.get("Guid", []))
                        target_ids = _resolve_target(providers, rk_ns, item_assignments, provider_assignments, all_account_ids)
                        if not target_ids:
                            continue
                        enabled_since = adc.get_enabled_since(db, cfg, lib_ns, movie_id=rk_ns)
                        owner_saw, owner_ts = _owner_state(i)
                        if not adc.candidate_ok(db, rk_ns, "movie", providers, target_ids, grace_days, enabled_since,
                                                owner_id=own, owner_saw=owner_saw, owner_ts=owner_ts, min_delay_minutes=min_delay):
                            continue
                        try:
                            plex_delete(f"/library/metadata/{rk_raw}")
                            logger.info(f"Auto-deleted movie {rk_raw} '{i.get('title', rk_raw)}' (sweep)")
                            deleted += 1
                        except Exception as e:
                            logger.warning(f"Sweep auto-delete failed movie {rk_raw}: {e}")
                        time.sleep(0.1)
                elif lib_type == "show":
                    ep_mc = plex_get(f"/library/sections/{lib_raw}/all",
                                     {"type": 4, "X-Plex-Container-Size": 50000, "includeGuids": 1})
                    show_providers_cache = {}
                    for ep in ep_mc.get("Metadata", []):
                        rk_raw = str(ep.get("ratingKey", ""))
                        series_raw = str(ep.get("grandparentRatingKey", ""))
                        if not rk_raw or not series_raw:
                            continue
                        series_ns = _ns(series_raw)
                        if not adc.is_auto_delete_active(db, cfg, lib_ns, series_ns):
                            continue
                        if series_raw not in show_providers_cache:
                            try:
                                s_mc = plex_get(f"/library/metadata/{series_raw}", {"includeGuids": 1})
                                show_providers_cache[series_raw] = parse_plex_guids((s_mc.get("Metadata") or [{}])[0].get("Guid", []))
                            except Exception:
                                show_providers_cache[series_raw] = {}
                        target_ids = _resolve_target(show_providers_cache[series_raw], series_ns,
                                                     item_assignments, provider_assignments, all_account_ids)
                        if not target_ids:
                            continue
                        ep_providers = parse_plex_guids(ep.get("Guid", []))
                        enabled_since = adc.get_enabled_since(db, cfg, lib_ns, series_ns)
                        owner_saw, owner_ts = _owner_state(ep)
                        if not adc.candidate_ok(db, _ns(rk_raw), "episode", ep_providers, target_ids, grace_days, enabled_since,
                                                owner_id=own, owner_saw=owner_saw, owner_ts=owner_ts, min_delay_minutes=min_delay):
                            continue
                        try:
                            plex_delete(f"/library/metadata/{rk_raw}")
                            logger.info(f"Auto-deleted episode {rk_raw} '{ep.get('title', rk_raw)}' (sweep)")
                            deleted += 1
                        except Exception as e:
                            logger.warning(f"Sweep auto-delete failed episode {rk_raw}: {e}")
                        time.sleep(0.1)
            except Exception as e:
                logger.warning(f"Sweep error for library {lib_ns}: {e}")
    finally:
        db.close()
    return deleted


def auto_delete_status(scope, raw, cfg):
    ns = _ns(raw)
    db = get_db()
    row = db.execute("SELECT enabled FROM auto_delete_overrides WHERE scope=? AND scope_id=?", (scope, ns)).fetchone()
    db.close()
    override = None if row is None else bool(row["enabled"])
    library_ns = None
    try:
        mc = plex_get(f"/library/metadata/{raw}")
        lib = str((mc.get("Metadata") or [{}])[0].get("librarySectionID") or "")
        library_ns = _ns(lib) if lib else None
    except Exception:
        pass
    library_enabled = bool(library_ns and library_ns in [str(x) for x in (cfg.get("auto_delete_libraries") or [])])
    effective = override if override is not None else library_enabled
    return {"override": override, "effective": effective, "library_enabled": library_enabled,
            "global_enabled": bool(cfg.get("auto_delete_enabled")),
            "grace_days": int(cfg.get("auto_delete_grace_days") or 0)}


def enabled_at_for_override(raw, db):
    """Plex opt-ins have no owner-history backdating (parity with original build)."""
    return datetime.now(timezone.utc).isoformat()


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
        mc = plex_get(f"/library/metadata/{raw}", {"includeGuids": 1})
        m = (mc.get("Metadata") or [{}])[0]
        library_ns = _ns(str(m.get("librarySectionID"))) if m.get("librarySectionID") else ""
        ptype = m.get("type")
        item_type = "episode" if ptype == "episode" else ("movie" if ptype == "movie" else ptype or "unknown")
        item_title = m.get("title", raw)
        providers = parse_plex_guids(m.get("Guid") or [])
        series_ns = _ns(str(m.get("grandparentRatingKey"))) if (item_type == "episode" and m.get("grandparentRatingKey")) else None
    except Exception as e:
        out.update({"verdict": "PROVIDER_ERROR", "verdict_detail": f"Could not fetch metadata from Plex: {e}"})
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
        own = owner_id()
        item_assignments, provider_assignments = _load_assignment_maps(db)
        lookup_id = series_ns if (item_type == "episode" and series_ns) else rating_key
        show_providers = providers if item_type == "movie" else {}
        if item_type == "episode" and series_ns:
            try:
                s_mc = plex_get(f"/library/metadata/{idutil.raw_of(series_ns)}", {"includeGuids": 1})
                show_providers = parse_plex_guids((s_mc.get("Metadata") or [{}])[0].get("Guid") or [])
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
        owner_view = (m.get("viewCount") or 0) > 0
        watch_events = []
        all_watched = True
        latest_ts = None
        for aid in target_ids:
            found_by = None
            if bool(providers) and is_item_watched_pwe(providers, item_type, {aid}, played):
                found_by = "provider"
            elif aid in rk_watchers:
                found_by = "rating_key"
            elif own and aid == own and owner_view:
                found_by = "viewcount"
            else:
                all_watched = False
            user_ts = None
            if providers:
                for pt, pid in providers.items():
                    row = db.execute(
                        "SELECT updated_at FROM watch_events WHERE account_id=? AND provider_type=? AND provider_id=? AND item_type=? AND event_type='play'",
                        (aid, pt.lower(), str(pid), item_type)).fetchone()
                    if row and (user_ts is None or row["updated_at"] > user_ts):
                        user_ts = row["updated_at"]
            if user_ts is None:
                row = db.execute("SELECT updated_at FROM watch_events WHERE account_id=? AND rating_key=? AND event_type='play'", (aid, rating_key)).fetchone()
                if row:
                    user_ts = row["updated_at"]
            if user_ts is None and found_by == "viewcount":
                lva = m.get("lastViewedAt")
                if lva:
                    user_ts = ts_to_iso(lva)
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
def recently_added_scan(cfg):
    show_all = cfg.get("show_all_libraries", True)
    monitored = set(cfg.get("monitored_libraries", []))
    out = []
    try:
        sections = plex_sections()
    except Exception:
        return out
    for s in sections:
        stype = s.get("type")
        if stype not in ("show", "movie"):
            continue
        lib_ns = _ns(str(s["key"]))
        if not (show_all or lib_ns in monitored):
            continue
        plex_type = 1 if stype == "movie" else 2
        item_type = "movie" if stype == "movie" else "series"
        try:
            mc = plex_get(f"/library/sections/{s['key']}/all", {
                "type": plex_type, "sort": "addedAt:desc", "includeGuids": 1,
                "X-Plex-Container-Size": 200, "X-Plex-Container-Start": 0})
        except Exception:
            continue
        for i in mc.get("Metadata", []):
            rk = str(i.get("ratingKey", ""))
            if not rk:
                continue
            providers = parse_plex_guids(i.get("Guid", []))
            first_seen = ts_to_iso(i.get("addedAt")) or datetime.now(timezone.utc).isoformat()
            out.append({"providers": providers, "item_type": item_type, "library_id": lib_ns,
                        "rating_key": _ns(rk), "title": i.get("title", ""),
                        "year": i.get("year"), "first_seen": first_seen})
    return out


media_lookup.register(KEY, get_item_providers)
