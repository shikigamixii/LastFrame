"""Tests for filtering Plex accounts down to users who currently have access.

plex.tv and the PMS are mocked: /accounts on the server lists every account
that ever had access, plex.tv says who still does.
"""
import pytest

import plex_api
import plex_engine

MACHINE = "machine-abc"
ACCOUNTS = {"Account": [
    {"id": 0, "name": ""},                 # system account
    {"id": 1, "name": "owner"},            # server owner (local id)
    {"id": 1001, "name": "friend"},        # shared with this server
    {"id": 1002, "name": "unshared"},      # still a friend, share removed
    {"id": 1003, "name": "homeuser"},      # Plex Home member
    {"id": 1004, "name": "Renamed"},       # id differs, name matches roster
    {"id": 1005, "name": ""},              # deleted account
    {"id": 1006, "name": "gone"},          # no longer a friend at all
]}
USERS_XML = f"""<MediaContainer>
  <User id="1001" title="friend" username="friend"><Server machineIdentifier="{MACHINE}"/></User>
  <User id="1002" title="unshared" username="unshared"><Server machineIdentifier="other-server"/></User>
  <User id="9999" title="renamed" username="renamed"><Server machineIdentifier="{MACHINE}"/></User>
</MediaContainer>""".encode()
HOME = [{"id": "1003", "uuid": "u", "title": "homeuser", "protected": False, "admin": False}]


class _Resp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


@pytest.fixture
def plex(monkeypatch):
    plex_api._roster_cache.update(ts=0.0, value=None)
    state = {"tv_up": True}

    def fake_get(path, params=None):
        return {"machineIdentifier": MACHINE} if path == "/" else ACCOUNTS

    def fake_session_get(url, **kw):
        if not state["tv_up"]:
            raise ConnectionError("plex.tv unreachable")
        return _Resp(USERS_XML)

    def fake_home():
        if not state["tv_up"]:
            raise ConnectionError("plex.tv unreachable")
        return HOME

    monkeypatch.setattr(plex_api, "plex_get", fake_get)
    monkeypatch.setattr(plex_api._session, "get", fake_session_get)
    monkeypatch.setattr(plex_api, "plex_tv_home_users", fake_home)
    monkeypatch.setattr(plex_api, "get_plex_token", lambda: "tok")
    monkeypatch.setattr(plex_api, "load_config", lambda: {"hidden_accounts": []})
    monkeypatch.setattr(plex_engine, "load_config", lambda: {"hidden_accounts": []})
    yield state
    plex_api._roster_cache.update(ts=0.0, value=None)


def test_only_current_users_are_tracked(plex):
    names = [a["name"] for a in plex_api.plex_accounts()]
    assert names == ["owner", "friend", "homeuser", "Renamed"]


def test_manage_users_flags_former(plex):
    former = {a["name"]: a["former"] for a in plex_engine.all_accounts()}
    assert former["owner"] is False and former["friend"] is False
    assert former["homeuser"] is False and former["Renamed"] is False
    assert former["unshared"] is True and former["gone"] is True
    assert former["(unnamed #1005)"] is True


def test_plex_tv_never_reachable_filters_nothing(plex):
    plex["tv_up"] = False
    names = [a["name"] for a in plex_api.plex_accounts()]
    assert names == ["owner", "friend", "unshared", "homeuser", "Renamed", "gone"]
    assert not any(a["former"] for a in plex_engine.all_accounts())


def test_plex_tv_outage_reuses_last_roster(plex):
    plex_api.plex_accounts()                      # caches a good roster
    plex["tv_up"] = False
    plex_api._roster_cache["ts"] = 0.0            # force a refresh attempt
    names = [a["name"] for a in plex_api.plex_accounts()]
    assert "gone" not in names and "friend" in names


def test_hidden_still_applies(plex, monkeypatch):
    monkeypatch.setattr(plex_api, "load_config", lambda: {"hidden_accounts": ["px_1001"]})
    assert "friend" not in [a["name"] for a in plex_api.plex_accounts()]
