"""Helpers for the watch_events store (webhook-fed play/stop events).

These query helpers accept an open `db` connection so callers control
transaction scope.
"""

def pwe_has_data(db):
    return db.execute("SELECT 1 FROM watch_events LIMIT 1").fetchone() is not None

def pwe_get_played(db, account_ids, item_types=None):
    """Batch query. Returns {(provider_type, provider_id, item_type): set(account_id)} for played events."""
    if not account_ids: return {}
    ph = ",".join("?" * len(account_ids))
    params = list(account_ids)
    q = f"SELECT account_id, provider_type, provider_id, item_type FROM watch_events WHERE account_id IN ({ph}) AND event_type='play'"
    if item_types:
        tph = ",".join("?" * len(item_types))
        q += f" AND item_type IN ({tph})"
        params.extend(item_types)
    rows = db.execute(q, params).fetchall()
    result = {}
    for r in rows:
        key = (r["provider_type"], r["provider_id"], r["item_type"])
        result.setdefault(key, set()).add(r["account_id"])
    return result

def pwe_get_played_with_ts(db, account_ids, item_types=None):
    """Like pwe_get_played but includes updated_at. Returns {(ptype, pid, itype, account_id): updated_at}"""
    if not account_ids: return {}
    ph = ",".join("?" * len(account_ids))
    params = list(account_ids)
    q = f"SELECT account_id, provider_type, provider_id, item_type, updated_at FROM watch_events WHERE account_id IN ({ph}) AND event_type='play'"
    if item_types:
        tph = ",".join("?" * len(item_types))
        q += f" AND item_type IN ({tph})"
        params.extend(item_types)
    rows = db.execute(q, params).fetchall()
    return {(r["provider_type"], r["provider_id"], r["item_type"], r["account_id"]): r["updated_at"] for r in rows}

def pwe_get_played_by_ratingkey(db, account_ids, item_types=None):
    """Returns {rating_key: set(account_id)} for play events that have a stored rating_key."""
    if not account_ids: return {}
    ph = ",".join("?" * len(account_ids))
    params = list(account_ids)
    q = f"SELECT account_id, rating_key FROM watch_events WHERE account_id IN ({ph}) AND event_type='play' AND rating_key != ''"
    if item_types:
        tph = ",".join("?" * len(item_types))
        q += f" AND item_type IN ({tph})"
        params.extend(item_types)
    rows = db.execute(q, params).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r["rating_key"], set()).add(r["account_id"])
    return result

def pwe_resolve_rating_keys(db, sections, item_type, missing):
    """No-op for Jellyfin — webhook events always carry ItemId as rating_key."""
    return {}

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
