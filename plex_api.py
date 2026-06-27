"""Plex Media Server HTTP client and helpers.

URL/token/secret are resolved at call time so changes via the setup wizard
take effect without restarting the app.
"""
import os, time
from datetime import datetime, timezone
import requests as http_requests

from config_store import load_config

# Reuse one connection pool for all Plex/plex.tv calls. The PMS is often remote
# (.plex.direct over TLS), so a fresh TCP+TLS handshake per request adds real
# latency; a shared Session keeps connections alive. requests.Session is safe
# to share across threads for issuing requests.
_session = http_requests.Session()

PLEX_HEADERS = {"Accept": "application/json"}

# plex.tv (not the local PMS) requires every request to identify the calling
# client. The listing endpoints tolerate the absence of these headers, but
# /api/v2/home/users/<id>/switch (and most v2 endpoints) reject calls that
# lack a stable X-Plex-Client-Identifier. We generate one on first use and
# keep it constant for the process lifetime.
import uuid as _uuid
_CLIENT_ID = _uuid.uuid4().hex
PLEX_TV_HEADERS = {
    "Accept": "application/json",
    "X-Plex-Product": "LastFrame",
    "X-Plex-Version": "1.0",
    "X-Plex-Client-Identifier": _CLIENT_ID,
    "X-Plex-Platform": "Python",
    "X-Plex-Device": "LastFrame Server",
    "X-Plex-Device-Name": "LastFrame",
}

_request_hook = None

def set_request_hook(fn):
    """Install fn(path, duration_seconds), called after each Plex HTTP request.

    Used by app.py to attribute Plex calls to the originating Flask request.
    """
    global _request_hook
    _request_hook = fn

def get_plex_url():
    return os.environ.get("PLEX_URL") or load_config().get("plex_url", "http://127.0.0.1:32400")

def get_plex_token():
    return os.environ.get("PLEX_TOKEN") or load_config().get("plex_token", "")

def get_webhook_secret():
    return os.environ.get("PLEX_WEBHOOK_SECRET") or load_config().get("webhook_secret", "")

def plex_get(path, params=None):
    p = {"X-Plex-Token": get_plex_token()}
    if params: p.update(params)
    t0 = time.monotonic()
    try:
        r = _session.get(f"{get_plex_url()}{path}", headers=PLEX_HEADERS, params=p, timeout=(5, 20))
        r.raise_for_status()
        # Remote .plex.direct hosts occasionally return HTTP 200 with an empty body
        # after a stall; treat that as "no data" rather than crashing on r.json().
        if not r.content:
            return {}
        try:
            return r.json().get("MediaContainer", {})
        except ValueError:
            return {}
    finally:
        if _request_hook is not None:
            try: _request_hook(path, time.monotonic() - t0)
            except Exception: pass

def plex_get_with_token(path, token, params=None):
    """Same as plex_get but with an explicit X-Plex-Token (per-user queries).

    Used by the managed-user sweep to read per-user viewCount/lastViewedAt,
    which Plex only returns when queried with that specific user's token.
    No request hook — these calls aren't tied to a Flask request.
    """
    p = {"X-Plex-Token": token}
    if params: p.update(params)
    r = _session.get(f"{get_plex_url()}{path}", headers=PLEX_HEADERS, params=p, timeout=(5, 20))
    r.raise_for_status()
    if not r.content:
        return {}
    try:
        return r.json().get("MediaContainer", {})
    except ValueError:
        return {}

def _plex_tv_headers(extra=None):
    h = dict(PLEX_TV_HEADERS)
    tk = get_plex_token()
    if tk:
        h["X-Plex-Token"] = tk
    if extra:
        h.update(extra)
    return h

def plex_tv_home_users():
    """Return [{'id','uuid','title','protected','admin'}] for each Plex Home user.

    Talks to plex.tv (not the local PMS) because the Home roster lives in the
    cloud. The legacy /api/home/users endpoint serves XML; we parse with stdlib.
    """
    from xml.etree import ElementTree as ET
    if not get_plex_token():
        return []
    r = _session.get(
        "https://plex.tv/api/home/users",
        headers=_plex_tv_headers({"Accept": "application/xml"}),
        timeout=15,
    )
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for u in root.findall("User"):
        out.append({
            "id": u.attrib.get("id"),
            "uuid": u.attrib.get("uuid"),
            "title": (u.attrib.get("title") or "").strip(),
            "protected": u.attrib.get("protected") == "1",
            "admin": u.attrib.get("admin") == "1",
        })
    return out

def plex_tv_switch_token_diag(home_user_id):
    """Try to mint a per-user auth token. Returns {'token','status','endpoint','body_preview'}.

    Tries the legacy XML endpoint first because the token it returns is a
    long-lived user authentication token that the PMS itself accepts; the
    v2 endpoint's authToken sometimes only authorises plex.tv API calls
    and gets 401'd by the local server. Falls back to v2 if legacy fails.
    """
    from xml.etree import ElementTree as ET
    diag = {"token": None, "status": None, "endpoint": None, "body_preview": None}
    if not get_plex_token() or not home_user_id:
        diag["body_preview"] = "no token or home_user_id"
        return diag

    # 1) Legacy XML endpoint — returns the user's master auth token, usable against the PMS.
    try:
        r = _session.post(
            f"https://plex.tv/api/home/users/{home_user_id}/switch",
            headers=_plex_tv_headers({"Accept": "application/xml"}),
            timeout=15,
        )
        diag["endpoint"] = "legacy"
        diag["status"] = r.status_code
        if r.status_code < 400 and r.content:
            try:
                root = ET.fromstring(r.content)
                tk = root.attrib.get("authenticationToken") or root.attrib.get("authToken")
                if tk:
                    diag["token"] = tk
                    return diag
            except ET.ParseError:
                pass
        diag["body_preview"] = (r.text or "")[:300]
    except Exception as e:
        diag["body_preview"] = f"legacy request error: {e}"

    # 2) v2 JSON fallback. Some Plex.tv accounts get 401/404 on legacy and v2 is the
    # only path that responds.
    try:
        r = _session.post(
            f"https://plex.tv/api/v2/home/users/{home_user_id}/switch",
            headers=_plex_tv_headers(),
            timeout=15,
        )
        diag["endpoint"] = "v2"
        diag["status"] = r.status_code
        if r.status_code < 400 and r.content:
            try:
                data = r.json()
                tk = data.get("authToken") or data.get("authenticationToken")
                if tk:
                    diag["token"] = tk
                    return diag
            except ValueError:
                pass
        diag["body_preview"] = (r.text or "")[:300]
    except Exception as e:
        diag["body_preview"] = f"v2 request error: {e}"

    return diag

def plex_tv_switch_token(home_user_id):
    """Mint a per-user auth token. Returns the token string or None."""
    return plex_tv_switch_token_diag(home_user_id).get("token")

def plex_refresh_access_tokens():
    """Force the PMS to pull the latest authorized-users list from plex.tv.

    Without this, freshly-minted per-user switch tokens are 401'd by the PMS
    because its in-memory authorized-tokens list is stale. Returns the HTTP
    status (or None on transport error) so callers can surface it.
    """
    token = get_plex_token()
    url = get_plex_url()
    if not token or not url:
        return None
    try:
        r = _session.post(
            f"{url}/myplex/refreshAccessTokens",
            headers=PLEX_HEADERS,
            params={"X-Plex-Token": token},
            timeout=15,
        )
        return r.status_code
    except Exception:
        return None

def plex_delete(path):
    r = _session.delete(f"{get_plex_url()}{path}", headers=PLEX_HEADERS,
                              params={"X-Plex-Token": get_plex_token()}, timeout=30)
    r.raise_for_status()
    return r

def plex_get_raw(path, params=None):
    p = {"X-Plex-Token": get_plex_token()}
    if params: p.update(params)
    r = _session.get(f"{get_plex_url()}{path}", params=p, timeout=15, stream=True)
    r.raise_for_status()
    return r

def plex_accounts():
    cfg = load_config()
    hidden = set(cfg.get("hidden_accounts", []))
    mc = plex_get("/accounts")
    return [{"id": str(a["id"]), "name": (a.get("name") or a.get("title") or "").strip()}
            for a in mc.get("Account", [])
            if a.get("id", 0) != 0
            and (a.get("name") or a.get("title") or "").strip()
            and str(a["id"]) not in hidden]

def plex_all_accounts():
    """All non-system accounts regardless of name or hidden status"""
    mc = plex_get("/accounts")
    return [{"id": str(a["id"]), "name": (a.get("name") or a.get("title") or "").strip()}
            for a in mc.get("Account", []) if a.get("id", 0) != 0]

def plex_owner_id():
    """The token owner's account ID — Plex viewCount data is only accurate for this account."""
    try:
        mc = plex_get("/accounts")
        ids = sorted([int(a["id"]) for a in mc.get("Account", []) if a.get("id", 0) > 0])
        return str(ids[0]) if ids else None
    except Exception:
        return None

_acct_name_cache: dict = {}
_acct_cache_ts: float = 0.0
_ACCT_CACHE_TTL = 300  # 5 minutes

def _local_account_id(cloud_id: str, cloud_name: str) -> str:
    """Map a Plex.tv cloud account ID to the local Plex server account ID by display-name match.

    Plex webhooks send the cloud account ID (an 8-digit Plex.tv ID) which differs from
    the local server account ID (usually a small integer like 1). Matching by display
    name normalises webhook events so they land under the same ID used by /accounts and
    the auto-delete candidate logic.
    """
    global _acct_name_cache, _acct_cache_ts
    now = time.monotonic()
    if now - _acct_cache_ts > _ACCT_CACHE_TTL:
        try:
            mc = plex_get("/accounts")
            _acct_name_cache = {
                (a.get("name") or a.get("title") or "").strip(): str(a["id"])
                for a in mc.get("Account", []) if a.get("id", 0) > 0
            }
            _acct_cache_ts = now
        except Exception:
            pass
    name = (cloud_name or "").strip()
    return _acct_name_cache.get(name) or cloud_id

def plex_sections():
    mc = plex_get("/library/sections")
    return mc.get("Directory", [])

def plex_genre_id(section_key, genre_name):
    try:
        mc = plex_get(f"/library/sections/{section_key}/genre")
        for g in mc.get("Directory", []):
            name = g.get("title") or g.get("tag") or ""
            if name.lower() == genre_name.lower():
                key = g.get("fastKey") or g.get("key") or ""
                # key may be "/library/sections/1/genre/9" or ".../all?genre=9"
                if "genre=" in key:
                    return key.split("genre=")[1].split("&")[0]
                return key.rstrip("/").split("/")[-1] or None
    except Exception:
        pass
    return None

def ts_to_iso(ts):
    if not ts: return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

def parse_plex_guids(guids):
    providers = {}
    for g in guids:
        gid = g.get("id", "")
        if gid.startswith("tmdb://"): providers["Tmdb"] = gid[7:]
        elif gid.startswith("tvdb://"): providers["Tvdb"] = gid[7:]
        elif gid.startswith("imdb://"): providers["Imdb"] = gid[7:]
    return providers

def get_item_providers(item_id):
    try:
        mc = plex_get(f"/library/metadata/{item_id}")
        item = (mc.get("Metadata") or [{}])[0]
        return parse_plex_guids(item.get("Guid", []))
    except Exception:
        return {}
