"""Helpers for the plex_watch_events store (webhook-fed play/stop events).

These query helpers accept an open `db` connection so callers control
transaction scope.
"""
from plex_api import plex_get

def pwe_has_data(db):
    return db.execute("SELECT 1 FROM plex_watch_events LIMIT 1").fetchone() is not None

def pwe_get_played(db, account_ids, item_types=None):
    """Batch query. Returns {(provider_type, provider_id, item_type): set(account_id)} for played events."""
    if not account_ids: return {}
    ph = ",".join("?" * len(account_ids))
    params = list(account_ids)
    q = f"SELECT plex_account_id, provider_type, provider_id, item_type FROM plex_watch_events WHERE plex_account_id IN ({ph}) AND event_type='play'"
    if item_types:
        tph = ",".join("?" * len(item_types))
        q += f" AND item_type IN ({tph})"
        params.extend(item_types)
    rows = db.execute(q, params).fetchall()
    result = {}
    for r in rows:
        key = (r["provider_type"], r["provider_id"], r["item_type"])
        result.setdefault(key, set()).add(r["plex_account_id"])
    return result

def pwe_get_played_with_ts(db, account_ids, item_types=None):
    """Like pwe_get_played but includes updated_at. Returns {(ptype, pid, itype, account_id): updated_at}"""
    if not account_ids: return {}
    ph = ",".join("?" * len(account_ids))
    params = list(account_ids)
    q = f"SELECT plex_account_id, provider_type, provider_id, item_type, updated_at FROM plex_watch_events WHERE plex_account_id IN ({ph}) AND event_type='play'"
    if item_types:
        tph = ",".join("?" * len(item_types))
        q += f" AND item_type IN ({tph})"
        params.extend(item_types)
    rows = db.execute(q, params).fetchall()
    return {(r["provider_type"], r["provider_id"], r["item_type"], r["plex_account_id"]): r["updated_at"] for r in rows}

def pwe_get_played_by_ratingkey(db, account_ids, item_types=None):
    """Returns {rating_key: set(plex_account_id)} for play events that have a stored rating_key."""
    if not account_ids: return {}
    ph = ",".join("?" * len(account_ids))
    params = list(account_ids)
    q = f"SELECT plex_account_id, rating_key FROM plex_watch_events WHERE plex_account_id IN ({ph}) AND event_type='play' AND rating_key != ''"
    if item_types:
        tph = ",".join("?" * len(item_types))
        q += f" AND item_type IN ({tph})"
        params.extend(item_types)
    rows = db.execute(q, params).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r["rating_key"], set()).add(r["plex_account_id"])
    return result

def pwe_resolve_rating_keys(db, sections, item_type, missing):
    """Resolve (provider_type, provider_id) -> rating_key for events missing rating_key.
    Scans the given sections, returns a dict and backfills rating_key in the DB."""
    if not missing: return {}
    plex_type = 1 if item_type == "movie" else 4
    found = {}
    for sec in sections:
        try:
            mc = plex_get(f"/library/sections/{sec['key']}/all",
                          {"type": plex_type, "X-Plex-Container-Size": 50000, "includeGuids": 1})
            for it in mc.get("Metadata", []):
                rk = str(it.get("ratingKey", ""))
                if not rk: continue
                for g in it.get("Guid", []):
                    gid = g.get("id", "")
                    if "://" in gid:
                        ptype_raw, pid = gid.split("://", 1)
                        key = (ptype_raw.lower(), pid)
                        if key in missing and key not in found:
                            found[key] = rk
        except Exception: continue
    for (ptype, pid), rk in found.items():
        db.execute("""UPDATE plex_watch_events SET rating_key=?
                      WHERE provider_type=? AND provider_id=? AND item_type=? AND rating_key=''""",
                   (rk, ptype, pid, item_type))
    db.commit()
    return found

def is_item_watched_pwe(providers, item_type, target_account_ids, played):
    """Check if ALL target accounts have a play event for this item."""
    if not target_account_ids or not providers: return False
    itype = item_type.lower()
    for aid in target_account_ids:
        found = any(aid in played.get((pt.lower(), str(pid), itype), set())
                    for pt, pid in providers.items())
        if not found: return False
    return True

def get_last_played_pwe(providers, item_type, account_id, played_ts):
    """Return the most recent updated_at timestamp for this account+item, or None."""
    itype = item_type.lower()
    ts = None
    for pt, pid in providers.items():
        t = played_ts.get((pt.lower(), str(pid), itype, account_id))
        if t and (ts is None or t > ts):
            ts = t
    return ts
