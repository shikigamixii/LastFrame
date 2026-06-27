"""Jellyfin HTTP client and helpers.

URL/api-key/webhook-secret are resolved at call time so changes via the
setup wizard take effect without restarting the app.
"""
import os, time
from datetime import datetime, timezone
import requests as http_requests

from config_store import load_config

# Reuse one connection pool for all Jellyfin calls. The server can be remote
# over TLS, so a fresh TCP+TLS handshake per request adds latency; a shared
# Session keeps connections alive. requests.Session is safe to share across
# threads for issuing requests.
_session = http_requests.Session()

_request_hook = None

def set_request_hook(fn):
    """Install fn(path, duration_seconds), called after each Jellyfin HTTP request.

    Used by app.py to attribute Jellyfin calls to the originating Flask request.
    """
    global _request_hook
    _request_hook = fn

def get_jellyfin_url():
    return os.environ.get("JELLYFIN_URL") or load_config().get("jellyfin_url", "http://127.0.0.1:8096")

def get_jellyfin_api_key():
    return os.environ.get("JELLYFIN_API_KEY") or load_config().get("jellyfin_api_key", "")

def get_webhook_secret():
    return os.environ.get("JELLYFIN_WEBHOOK_SECRET") or load_config().get("webhook_secret", "")

def _jf_headers():
    return {"Accept": "application/json", "X-Emby-Token": get_jellyfin_api_key()}

def jellyfin_get(path, params=None):
    t0 = time.monotonic()
    try:
        r = _session.get(f"{get_jellyfin_url()}{path}", headers=_jf_headers(), params=params or {}, timeout=(5, 20))
        r.raise_for_status()
        # Jellyfin can occasionally return HTTP 200 with an empty body during
        # a connection stall; treat that as "no data" rather than crashing on r.json().
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}
    finally:
        if _request_hook is not None:
            try: _request_hook(path, time.monotonic() - t0)
            except Exception: pass

def jellyfin_delete(item_id):
    r = _session.delete(f"{get_jellyfin_url()}/Items/{item_id}", headers=_jf_headers(), timeout=30)
    r.raise_for_status()
    return r

def jellyfin_users():
    """Visible accounts (hidden filtered out)."""
    cfg = load_config()
    hidden = set(cfg.get("hidden_accounts", []))
    return [{"id": u["Id"], "name": u["Name"]}
            for u in jellyfin_get("/Users")
            if u.get("Name") and u["Id"] not in hidden]

def jellyfin_all_users():
    """All users regardless of hidden status."""
    return [{"id": u["Id"], "name": u["Name"]}
            for u in jellyfin_get("/Users") if u.get("Name")]

_admin_id_cache = None

def jellyfin_admin_id():
    """First admin user ID — used for UserData watch-status fallback.

    Cached for the process lifetime; newer Jellyfin versions require a
    userId on /Items/{id} calls, so this gets hit frequently.
    """
    global _admin_id_cache
    if _admin_id_cache is not None:
        return _admin_id_cache
    try:
        for u in jellyfin_get("/Users"):
            if u.get("Policy", {}).get("IsAdministrator"):
                _admin_id_cache = u["Id"]
                return _admin_id_cache
    except Exception:
        return None


def jellyfin_get_item(item_id, params=None, admin_id=None):
    """GET /Items/{id} with userId injected.

    Newer Jellyfin versions return 400 Bad Request for /Items/{id} without
    a userId query parameter. Callers that already pass an explicit userId
    in `params` are not overridden. Pass `admin_id` explicitly when calling
    in a loop to avoid the (cached) lookup on every iteration.
    """
    p = dict(params or {})
    if "userId" not in p:
        aid = admin_id if admin_id is not None else jellyfin_admin_id()
        if aid:
            p["userId"] = aid
    return jellyfin_get(f"/Items/{item_id}", p)

def jellyfin_libraries():
    """List Jellyfin media libraries."""
    data = jellyfin_get("/Library/MediaFolders")
    return data.get("Items", [])

def jellyfin_items(params):
    """Generic item query returning Items list."""
    data = jellyfin_get("/Items", params)
    return data.get("Items", [])

def ts_to_iso(ts):
    if not ts: return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

def parse_jellyfin_providers(provider_ids):
    """Convert Jellyfin ProviderIds dict to internal format {Tmdb/Tvdb/Imdb: id}."""
    result = {}
    if not provider_ids: return result
    for key in ("Tmdb", "Tvdb", "Imdb"):
        if provider_ids.get(key):
            result[key] = str(provider_ids[key])
    return result

def get_item_providers(item_id):
    try:
        data = jellyfin_get_item(item_id, {"fields": "ProviderIds"})
        return parse_jellyfin_providers(data.get("ProviderIds") or {})
    except Exception:
        return {}
