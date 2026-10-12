"""Tests for the version / update-available check. GitHub is always mocked."""
import pytest

import update_check as uc


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


@pytest.fixture
def gh(monkeypatch):
    uc._cache.update(checked_at=0.0, ok=False, latest=None)
    state = {"calls": 0, "resp": _Resp(200, {"tag_name": "v2.3.0", "name": "LastFrame v2.3.0",
                                              "html_url": "https://github.com/shikigamixii/LastFrame/releases/tag/v2.3.0"})}

    def fake_get(url, **kw):
        state["calls"] += 1
        if isinstance(state["resp"], Exception):
            raise state["resp"]
        return state["resp"]
    monkeypatch.setattr(uc.http_requests, "get", fake_get)
    monkeypatch.setattr(uc, "current_version", lambda: "2.2.0")
    monkeypatch.setattr(uc, "load_config", lambda: {})
    monkeypatch.delenv("LASTFRAME_UPDATE_CHECK", raising=False)
    yield state
    uc._cache.update(checked_at=0.0, ok=False, latest=None)


def test_version_ordering():
    assert uc.is_newer("2.2.0", "2.1.0")
    assert uc.is_newer("v2.10", "2.9.9")
    assert uc.is_newer("2.2.0", "2.2.0-dev")          # release beats its own dev build
    assert not uc.is_newer("2.1.0", "2.2.0-dev")      # dev build is ahead of older release
    assert not uc.is_newer("2.2.0", "2.2.0")
    assert not uc.is_newer("Plex-v1.2", "1.0.0")      # old provider tags are ignored
    assert not uc.is_newer("2.3.0", "unknown")


def test_update_available(gh):
    s = uc.status()
    assert s["version"] == "2.2.0" and s["update_available"] is True
    assert s["latest"]["version"] == "2.3.0" and s["latest"]["url"].endswith("/v2.3.0")


def test_no_update_when_current(gh, monkeypatch):
    monkeypatch.setattr(uc, "current_version", lambda: "2.3.0")
    assert uc.status()["update_available"] is False


def test_checks_github_once_a_day(gh):
    uc.latest_release(now=1000.0)
    uc.latest_release(now=1000.0 + 3600)
    assert gh["calls"] == 1
    uc.latest_release(now=1000.0 + uc.CHECK_INTERVAL + 1)
    assert gh["calls"] == 2


def test_failure_keeps_last_answer_and_retries_sooner(gh):
    uc.latest_release(now=1000.0)
    gh["resp"] = ConnectionError("offline")
    t = 1000.0 + uc.CHECK_INTERVAL + 1
    assert uc.latest_release(now=t)["version"] == "2.3.0"          # last good answer kept
    gh["resp"] = _Resp(200, {"tag_name": "v2.4.0", "html_url": "https://github.com/shikigamixii/LastFrame/releases/tag/v2.4.0"})
    assert uc.latest_release(now=t + uc.RETRY_INTERVAL + 1)["version"] == "2.4.0"


def test_no_release_or_odd_tag_means_no_update(gh):
    gh["resp"] = _Resp(404)
    assert uc.status()["update_available"] is False
    uc._cache.update(checked_at=0.0)
    gh["resp"] = _Resp(200, {"tag_name": "Plex-v9.9"})
    assert uc.status()["latest"] is None


def test_foreign_release_url_replaced(gh):
    gh["resp"] = _Resp(200, {"tag_name": "v2.3.0", "html_url": "https://evil.example/x"})
    assert uc.status()["latest"]["url"] == "https://github.com/shikigamixii/LastFrame/releases"


def test_disabled_by_setting_or_env_never_calls_github(gh, monkeypatch):
    monkeypatch.setattr(uc, "load_config", lambda: {"update_check_enabled": False})
    s = uc.status()
    assert s["check_enabled"] is False and s["latest"] is None and gh["calls"] == 0
    monkeypatch.setattr(uc, "load_config", lambda: {"update_check_enabled": True})
    monkeypatch.setenv("LASTFRAME_UPDATE_CHECK", "0")
    assert uc.status()["check_enabled"] is False and gh["calls"] == 0


def test_version_file_is_valid():
    v = open(uc.os.path.join(uc.APP_DIR, "VERSION")).read().strip()
    assert uc.parse_version(v) is not None


def test_version_endpoint_requires_login():
    import os
    os.environ.setdefault("RECENTLY_ADDED_SWEEP_INTERVAL", "0")
    os.environ.setdefault("PLEX_HISTORY_POLL_INTERVAL", "0")
    import app as A
    assert A.app.test_client().get("/api/version").status_code == 401
