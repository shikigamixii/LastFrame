"""The Plex token must travel in the X-Plex-Token header, never in the URL,
so it can't leak through error messages, proxy logs or access logs."""
import pytest

import plex_api

TOKEN = "FAKE_PLEX_TOKEN_abc123"


class _Resp:
    status_code = 200
    content = b'{"MediaContainer": {"size": 0}}'
    headers = {"Content-Type": "application/json"}

    def raise_for_status(self):
        pass

    def json(self):
        return {"MediaContainer": {"size": 0}}


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def record(method):
        def fn(url, headers=None, params=None, **kw):
            seen.append({"method": method, "url": url, "headers": headers or {}, "params": params or {}})
            return _Resp()
        return fn

    monkeypatch.setattr(plex_api._session, "get", record("GET"))
    monkeypatch.setattr(plex_api._session, "post", record("POST"))
    monkeypatch.setattr(plex_api._session, "delete", record("DELETE"))
    monkeypatch.setattr(plex_api, "get_plex_token", lambda: TOKEN)
    monkeypatch.setattr(plex_api, "get_plex_url", lambda: "https://pms.example:32400")
    return seen


def _assert_header_only(call, token=TOKEN):
    assert call["headers"].get("X-Plex-Token") == token
    assert token not in call["url"]
    assert "X-Plex-Token" not in call["params"]
    assert token not in str(call["params"])


def test_all_pms_calls_use_header(calls):
    plex_api.plex_get("/library/sections", {"type": 1})
    plex_api.plex_get_with_token("/library/sections/1/all", "USER_TOKEN_xyz", {"type": 1})
    plex_api.plex_delete("/library/metadata/5")
    plex_api.plex_get_raw("/photo/:/transcode", {"width": "300"})
    plex_api.plex_refresh_access_tokens()
    assert [c["method"] for c in calls] == ["GET", "GET", "DELETE", "GET", "POST"]
    _assert_header_only(calls[0])
    _assert_header_only(calls[1], token="USER_TOKEN_xyz")
    for c in calls[2:]:
        _assert_header_only(c)
    assert calls[0]["params"] == {"type": 1}           # caller params still sent
    assert calls[0]["headers"]["Accept"] == "application/json"


def test_caller_params_not_mutated(calls):
    params = {"type": 1}
    plex_api.plex_get("/x", params)
    assert params == {"type": 1}


def test_redact_obj_masks_nested_error_text():
    import os
    os.environ.setdefault("RECENTLY_ADDED_SWEEP_INTERVAL", "0")
    os.environ.setdefault("PLEX_HISTORY_POLL_INTERVAL", "0")
    import app as A
    out = A._redact_obj({"providers": {"plex": {"error": f"404 for url: https://h/x?X-Plex-Token={TOKEN}"}},
                         "users": [{"skipped_reason": f"failed: ?X-Plex-Token={TOKEN}&a=1"}], "n": 3})
    assert TOKEN not in str(out) and out["n"] == 3
