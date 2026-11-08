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


# ── manual watch resets ───────────────────────────────────────────────
def _parse_ts(value):
    """ISO timestamp -> aware UTC datetime, or None when it can't be read."""
    from datetime import datetime, timezone
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _item_keys(providers, rating_key):
    """(provider_type, provider_id) keys an item's watch_events rows can use."""
    keys = [(pt.lower(), str(pid)) for pt, pid in (providers or {}).items() if pid]
    if rating_key:
        keys.append(("ratingkey", str(rating_key)))
    return keys


def pwe_clear_item(db, account_id, item_type, providers, rating_key, reset_at=None):
    """Remove one account's play rows for one item. Returns rows deleted.

    Matches the item's provider ids and its namespaced rating key, the same
    keys the watched checks use. With reset_at, also records a reset for each
    key so history imports skip plays from before it (see pwe_reset_blocks).
    Does not commit.
    """
    keys = _item_keys(providers, rating_key)
    if not account_id or not item_type or not keys:
        return 0
    deleted = 0
    for ptype, pid in keys:
        if ptype != "ratingkey":
            cur = db.execute(
                "DELETE FROM watch_events WHERE account_id=? AND item_type=? AND provider_type=? AND provider_id=?",
                (account_id, item_type, ptype, pid))
            deleted += cur.rowcount
    if rating_key:
        cur = db.execute(
            "DELETE FROM watch_events WHERE account_id=? AND item_type=? AND rating_key=?",
            (account_id, item_type, str(rating_key)))
        deleted += cur.rowcount
    if reset_at:
        for ptype, pid in keys:
            db.execute(
                "INSERT INTO watch_resets (account_id, provider_type, provider_id, item_type, reset_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(account_id, provider_type, provider_id, item_type) DO UPDATE SET reset_at=excluded.reset_at",
                (account_id, ptype, pid, item_type, reset_at))
    return deleted


def pwe_mark_item(db, account_id, item_type, providers, rating_key, played_at):
    """Store a play for one account and item (manual "mark as watched").
    Returns rows added.

    Writes the same rows a play webhook would. A user who already has a play
    keeps it, along with its original date, so an auto-delete grace period
    already running isn't restarted. Any earlier reset of the item is
    dropped. Does not commit.
    """
    pkeys = [(pt.lower(), str(pid)) for pt, pid in (providers or {}).items() if pid]
    if not pkeys and rating_key:
        pkeys = [("ratingkey", str(rating_key))]
    if not account_id or not item_type or not pkeys:
        return 0
    added = 0
    for ptype, pid in pkeys:
        cur = db.execute(
            "INSERT INTO watch_events (account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key) "
            "VALUES (?, ?, ?, ?, 'play', ?, ?) "
            "ON CONFLICT(account_id, provider_type, provider_id, item_type) DO NOTHING",
            (account_id, ptype, pid, item_type, played_at, str(rating_key or "")))
        added += cur.rowcount
    for ptype, pid in _item_keys(providers, rating_key):
        db.execute("DELETE FROM watch_resets WHERE account_id=? AND provider_type=? AND provider_id=? AND item_type=?",
                   (account_id, ptype, pid, item_type))
    return added


def pwe_reset_blocks(db, account_id, item_type, providers, rating_key, played_at):
    """True when the item was reset for this account at or after played_at,
    so an imported play from server history must not be stored again."""
    keys = _item_keys(providers, rating_key)
    if not account_id or not keys:
        return False
    latest = None
    for ptype, pid in keys:
        row = db.execute(
            "SELECT reset_at FROM watch_resets WHERE account_id=? AND provider_type=? AND provider_id=? AND item_type=?",
            (account_id, ptype, pid, item_type)).fetchone()
        if row:
            ts = _parse_ts(row["reset_at"])
            if ts and (latest is None or ts > latest):
                latest = ts
    if latest is None:
        return False
    played = _parse_ts(played_at)
    # A play with no readable time can't be shown to be newer than the reset.
    return played is None or played <= latest
