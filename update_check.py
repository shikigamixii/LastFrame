"""App version and "update available" check against GitHub releases.

The running version comes from the VERSION file (develop builds carry a
"-dev" suffix, e.g. 2.2.0-dev). Once every CHECK_INTERVAL the server asks
GitHub for the latest published release and caches the answer; the browser
never contacts GitHub itself. Drafts and pre-releases are ignored by GitHub's
/releases/latest, and only tags shaped like v2.1 / v2.1.0 are compared, so
older tags such as Plex-v1.2 never count.

The check can be turned off in Settings (update_check_enabled) or with the
environment variable LASTFRAME_UPDATE_CHECK=0, which wins over Settings.
"""
import os
import re
import threading
import time

import requests as http_requests

from config_store import APP_DIR, load_config

REPO = "shikigamixii/LastFrame"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_INTERVAL = 24 * 3600
# After a failed check, retry sooner than a full day.
RETRY_INTERVAL = 3600

_VERSION_RE = re.compile(r'^v?(\d+)\.(\d+)(?:\.(\d+))?(?:-([0-9A-Za-z.]+))?$')

_lock = threading.Lock()
_cache = {"checked_at": 0.0, "ok": False, "latest": None}


def current_version():
    try:
        with open(os.path.join(APP_DIR, "VERSION")) as f:
            return f.read().strip() or "unknown"
    except OSError:
        return "unknown"


def parse_version(text):
    """(major, minor, patch, is_release) or None. A pre-release such as
    2.2.0-dev sorts before 2.2.0, which is what makes dev builds work."""
    m = _VERSION_RE.match((text or "").strip())
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    return (int(major), int(minor), int(patch or 0), pre is None)


def is_newer(candidate, current):
    a, b = parse_version(candidate), parse_version(current)
    return bool(a and b and a > b)


def check_enabled(cfg=None):
    env = os.environ.get("LASTFRAME_UPDATE_CHECK", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    cfg = cfg if cfg is not None else load_config()
    return bool(cfg.get("update_check_enabled", True))


def _fetch_latest():
    r = http_requests.get(LATEST_URL, timeout=5, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"LastFrame/{current_version()}",
    })
    if r.status_code == 404:          # no published release yet
        return None
    r.raise_for_status()
    data = r.json()
    tag = str(data.get("tag_name") or "")
    if not parse_version(tag):
        return None
    url = str(data.get("html_url") or "")
    if not url.startswith(f"https://github.com/{REPO}/"):
        url = f"https://github.com/{REPO}/releases"
    return {"version": tag.lstrip("v"), "tag": tag, "name": str(data.get("name") or tag), "url": url}


def latest_release(now=None):
    """Cached latest release (or None), refreshed at most once per interval."""
    now = now if now is not None else time.time()
    with _lock:
        age = now - _cache["checked_at"]
        if age < (CHECK_INTERVAL if _cache["ok"] else RETRY_INTERVAL) and _cache["checked_at"]:
            return _cache["latest"]
        try:
            _cache.update(latest=_fetch_latest(), ok=True)
        except Exception:
            _cache["ok"] = False      # keep the last good answer, retry later
        _cache["checked_at"] = now
        return _cache["latest"]


def status(cfg=None):
    version = current_version()
    out = {"version": version, "check_enabled": check_enabled(cfg),
           "latest": None, "update_available": False}
    if out["check_enabled"]:
        latest = latest_release()
        out["latest"] = latest
        out["update_available"] = bool(latest and is_newer(latest["version"], version))
    return out
