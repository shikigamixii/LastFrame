"""User-to-item assignment helpers.

Two storage modes:
- assignments_by_provider: provider-id keyed (Tmdb/Imdb/Tvdb), survives item-id changes
- assignments: item-id keyed fallback when no provider IDs are available
get_assigned_ids() prefers provider; falls back to item-id.
"""
from config_store import PROVIDER_PRIORITY
from db import get_db
from media_lookup import get_item_providers

def get_assignment_by_provider(item_id):
    providers = get_item_providers(item_id)
    if not providers: return None
    db = get_db()
    for ptype in PROVIDER_PRIORITY:
        pid = providers.get(ptype)
        if pid:
            rows = db.execute(
                "SELECT user_id FROM assignments_by_provider WHERE provider_type = ? AND provider_id = ?",
                (ptype, str(pid))
            ).fetchall()
            if rows:
                db.close()
                return set(r[0] for r in rows)
    db.close()
    return None

def set_assignment_by_provider(item_id, user_ids):
    providers = get_item_providers(item_id)
    if not providers: return False
    db = get_db()
    for ptype in PROVIDER_PRIORITY:
        pid = providers.get(ptype)
        if pid:
            db.execute("DELETE FROM assignments_by_provider WHERE provider_type = ? AND provider_id = ?",
                       (ptype, str(pid)))
            for uid in user_ids:
                db.execute("INSERT INTO assignments_by_provider (provider_type, provider_id, user_id) VALUES (?, ?, ?)",
                           (ptype, str(pid), uid))
            db.commit(); db.close()
            return True
    db.close()
    return False

def delete_assignment_by_provider(item_id):
    providers = get_item_providers(item_id)
    if not providers: return False
    db = get_db()
    for ptype in PROVIDER_PRIORITY:
        pid = providers.get(ptype)
        if pid:
            db.execute("DELETE FROM assignments_by_provider WHERE provider_type = ? AND provider_id = ?",
                       (ptype, str(pid)))
            db.commit(); db.close()
            return True
    db.close()
    return False

def get_assignment_by_item_id(item_id):
    db = get_db()
    rows = db.execute("SELECT user_id FROM assignments WHERE item_id = ?", (item_id,)).fetchall()
    db.close()
    return set(r[0] for r in rows) if rows else None

def set_assignment_by_item_id(item_id, user_ids):
    db = get_db()
    db.execute("DELETE FROM assignments WHERE item_id = ?", (item_id,))
    for uid in user_ids:
        db.execute("INSERT INTO assignments (item_id, user_id) VALUES (?, ?)", (item_id, uid))
    db.commit(); db.close()

def delete_assignment_by_item_id(item_id):
    db = get_db()
    db.execute("DELETE FROM assignments WHERE item_id = ?", (item_id,))
    db.commit(); db.close()

def get_assigned_ids(item_id):
    assigned = get_assignment_by_provider(item_id)
    if assigned is not None: return assigned
    return get_assignment_by_item_id(item_id)

# Shared by watch-summary and auto-delete
def _load_assignment_maps(db):
    rows = db.execute("SELECT item_id, user_id FROM assignments").fetchall()
    item_assignments = {}
    for row in rows:
        item_assignments.setdefault(row["item_id"], set()).add(row["user_id"])
    prov_rows = db.execute("SELECT provider_type, provider_id, user_id FROM assignments_by_provider").fetchall()
    provider_assignments = {}
    for row in prov_rows:
        provider_assignments.setdefault((row["provider_type"], row["provider_id"]), set()).add(row["user_id"])
    return item_assignments, provider_assignments

def _resolve_target(providers, item_id, item_assignments, provider_assignments, all_account_ids):
    for ptype in PROVIDER_PRIORITY:
        pid = (providers or {}).get(ptype)
        if pid and (ptype, str(pid)) in provider_assignments:
            return provider_assignments[(ptype, str(pid))]
    return item_assignments.get(item_id, all_account_ids)
