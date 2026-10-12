"""App version and "update available" check against GitHub releases.

The running version comes from the VERSION file. Each install follows the
channel of the branch it was built from, so it is only told about builds the
maintainer has marked as ready for that branch:

    stable  - published releases (e.g. v2.3.0); pre-releases are ignored
    develop - published *pre-releases* only (e.g. v2.2.0-dev.2); new commits
              and stable releases are never announced to develop installs

The branch is read from the git checkout (.git/HEAD, which the Docker build
keeps); without it - e.g. a detached checkout of a tag - a version with a
pre-release suffix ("-dev...") means develop, anything else stable.

Once every CHECK_INTERVAL the server asks GitHub and caches the answer; the
browser never contacts GitHub itself. Only tags shaped like v2.1 / v2.1.0 /
v2.2.0-dev.1 are compared, so older tags such as Plex-v1.2 never count.

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
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
CHECK_INTERVAL = 24 * 3600
# After a failed check, retry sooner than a full day.
RETRY_INTERVAL = 3600

_VERSION_RE = re.compile(r'^v?(\d+)\.(\d+)(?:\.(\d+))?(?:-([0-9A-Za-z.]+))?$')

_lock = threading.Lock()
_cache = {"checked_at": 0.0, "ok": False, "latest": None, "channel": None}


def current_version():
    try:
        with open(os.path.join(APP_DIR, "VERSION")) as f:
            return f.read().strip() or "unknown"
    except OSError:
        return "unknown"


def _pre_key(pre):
    """Semantic-versioning order for a pre-release suffix: numeric parts
    compare as numbers (dev.2 < dev.10), and fewer parts sort first
    (dev < dev.1)."""
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))


def parse_version(text):
    """A sortable key, or None if the text isn't a version. A pre-release
    (2.2.0-dev.1) sorts before its release (2.2.0) and after older releases."""
    m = _VERSION_RE.match((text or "").strip())
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    core = (int(major), int(minor), int(patch or 0))
    return core + ((1, ()) if pre is None else (0, _pre_key(pre)))


def is_prerelease(text):
    key = parse_version(text)
    return bool(key) and key[3] == 0


def is_newer(candidate, current):
    a, b = parse_version(candidate), parse_version(current)
    return bool(a and b and a > b)


def git_branch():
    """Branch name from the checkout's .git/HEAD, or None (no git data, or a
    detached checkout such as a tag)."""
    try:
        with open(os.path.join(APP_DIR, ".git", "HEAD")) as f:
            head = f.read().strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    return head[len(prefix):] if head.startswith(prefix) else None


def channel():
    branch = git_branch()
    if branch in ("stable", "develop"):
        return branch
    return "develop" if is_prerelease(current_version()) else "stable"


def check_enabled(cfg=None):
    env = os.environ.get("LASTFRAME_UPDATE_CHECK", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    cfg = cfg if cfg is not None else load_config()
    return bool(cfg.get("update_check_enabled", True))


def _gh_get(url):
    return http_requests.get(url, timeout=5, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"LastFrame/{current_version()}",
    })


def _release_info(data):
    tag = str(data.get("tag_name") or "")
    if not parse_version(tag):
        return None
    url = str(data.get("html_url") or "")
    if not url.startswith(f"https://github.com/{REPO}/"):
        url = f"https://github.com/{REPO}/releases"
    return {"version": tag.lstrip("v"), "tag": tag, "name": str(data.get("name") or tag), "url": url}


def _fetch_latest(chan="stable"):
    if chan == "develop":
        # Newest published pre-release (GitHub hides drafts from this list).
        r = _gh_get(RELEASES_URL)
        r.raise_for_status()
        best = None
        for data in r.json() or []:
            if not data.get("prerelease") or data.get("draft"):
                continue
            info = _release_info(data)
            if info and is_prerelease(info["version"]) and (
                    best is None or is_newer(info["version"], best["version"])):
                best = info
        return best
    r = _gh_get(LATEST_URL)            # excludes drafts and pre-releases
    if r.status_code == 404:          # no published release yet
        return None
    r.raise_for_status()
    info = _release_info(r.json())
    return info if info and not is_prerelease(info["version"]) else None


def latest_release(chan="stable", now=None):
    """Cached newest build for the channel (or None), refreshed at most once
    per interval."""
    now = now if now is not None else time.time()
    with _lock:
        if _cache["channel"] != chan:
            _cache.update(checked_at=0.0, ok=False, latest=None, channel=chan)
        age = now - _cache["checked_at"]
        if age < (CHECK_INTERVAL if _cache["ok"] else RETRY_INTERVAL) and _cache["checked_at"]:
            return _cache["latest"]
        try:
            _cache.update(latest=_fetch_latest(chan), ok=True)
        except Exception:
            _cache["ok"] = False      # keep the last good answer, retry later
        _cache["checked_at"] = now
        return _cache["latest"]


def status(cfg=None):
    version = current_version()
    chan = channel()
    out = {"version": version, "channel": chan, "check_enabled": check_enabled(cfg),
           "latest": None, "update_available": False}
    if out["check_enabled"]:
        latest = latest_release(chan)
        out["latest"] = latest
        out["update_available"] = bool(latest and is_newer(latest["version"], version))
    return out
