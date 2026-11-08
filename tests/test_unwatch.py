"""Tests for "mark as unwatched": clearing stored plays, the reset guard that
stops history imports from bringing them back, the Jellyfin unplayed webhook,
and the /api/unwatch routes. No media server is needed: server calls are
monkeypatched and each test uses its own temporary database.
"""
import os

os.environ.setdefault("RECENTLY_ADDED_SWEEP_INTERVAL", "0")
os.environ.setdefault("PLEX_HISTORY_POLL_INTERVAL", "0")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-for-unwatch-tests")

import pytest

import config_store
import db as db_mod
from webhook_state import pwe_clear_item, pwe_reset_blocks, pwe_get_played

SETUP_BODY = {"username": "owner", "password": "ownerpass1", "confirm_password": "ownerpass1",
              "jellyfin_url": "https://jf.example", "jellyfin_api_key": "FAKE_API_KEY_123",
              "webhook_secret": "S" * 20}


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "data.db"))
    db_mod.init_db()
    return db_mod


def _play(db, account, ptype, pid, item_type, ts, rk):
    db.execute(
        "INSERT INTO watch_events (account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key) "
        "VALUES (?, ?, ?, ?, 'play', ?, ?)", (account, ptype, pid, item_type, ts, rk))


def _seed(tmpdb):
    db = tmpdb.get_db()
    _play(db, "jf_a", "tmdb", "1", "movie", "2026-01-01T00:00:00+00:00", "jf_101")
    _play(db, "jf_a", "imdb", "tt1", "movie", "2026-01-01T00:00:00+00:00", "jf_101")
    _play(db, "jf_b", "tmdb", "1", "movie", "2026-01-01T00:00:00+00:00", "jf_101")
    _play(db, "jf_a", "tmdb", "2", "movie", "2026-01-01T00:00:00+00:00", "jf_102")
    _play(db, "jf_a", "ratingkey", "jf_103", "movie", "2026-01-01T00:00:00+00:00", "jf_103")
    db.commit()
    return db


# ── pwe_clear_item ────────────────────────────────────────────────────
def test_clear_item_only_touches_that_account_and_item(tmpdb):
    db = _seed(tmpdb)
    n = pwe_clear_item(db, "jf_a", "movie", {"Tmdb": "1", "Imdb": "tt1"}, "jf_101")
    db.commit()
    assert n == 2
    played = pwe_get_played(db, ["jf_a", "jf_b"], ["movie"])
    assert "jf_a" not in played.get(("tmdb", "1", "movie"), set())
    assert played[("tmdb", "1", "movie")] == {"jf_b"}
    assert played[("tmdb", "2", "movie")] == {"jf_a"}
    db.close()


def test_clear_item_matches_rating_key_only_rows(tmpdb):
    db = _seed(tmpdb)
    assert pwe_clear_item(db, "jf_a", "movie", {}, "jf_103") == 1
    db.close()


def test_clear_without_reset_at_records_no_reset(tmpdb):
    db = _seed(tmpdb)
    pwe_clear_item(db, "jf_a", "movie", {"Tmdb": "1"}, "jf_101")
    assert db.execute("SELECT COUNT(*) c FROM watch_resets").fetchone()["c"] == 0
    db.close()


# ── pwe_reset_blocks ──────────────────────────────────────────────────
def test_reset_blocks_older_plays_and_allows_newer(tmpdb):
    db = _seed(tmpdb)
    pwe_clear_item(db, "jf_a", "movie", {"Tmdb": "1"}, "jf_101", "2026-06-01T12:00:00.123456+00:00")
    db.commit()
    assert pwe_reset_blocks(db, "jf_a", "movie", {"Tmdb": "1"}, "jf_101", "2026-01-01T00:00:00+00:00")
    # Jellyfin's 7-digit fraction + Z format
    assert pwe_reset_blocks(db, "jf_a", "movie", {}, "jf_101", "2026-05-31T00:00:00.0000000Z")
    assert not pwe_reset_blocks(db, "jf_a", "movie", {"Tmdb": "1"}, "jf_101", "2026-07-01T00:00:00+00:00")
    # other user, other item: never blocked
    assert not pwe_reset_blocks(db, "jf_b", "movie", {"Tmdb": "1"}, "jf_101", "2026-01-01T00:00:00+00:00")
    assert not pwe_reset_blocks(db, "jf_a", "movie", {"Tmdb": "2"}, "jf_102", "2026-01-01T00:00:00+00:00")
    # unreadable play time can't be shown to be newer than the reset
    assert pwe_reset_blocks(db, "jf_a", "movie", {"Tmdb": "1"}, "", "not-a-date")
    db.close()


# ── Jellyfin unplayed webhook ─────────────────────────────────────────
class _Req:
    def __init__(self, body):
        self._body = body

    def get_json(self, force=False, silent=False):
        return self._body


def test_jellyfin_unplayed_webhook_clears_play(tmpdb):
    import jellyfin_engine as je
    db = tmpdb.get_db()
    _play(db, "jf_a", "tmdb", "1", "movie", "2026-01-01T00:00:00+00:00", "jf_101")
    _play(db, "jf_b", "tmdb", "1", "movie", "2026-01-01T00:00:00+00:00", "jf_101")
    db.commit()
    body, status = je.store_webhook(_Req({"NotificationType": "UserDataSaved", "Played": "false",
                                          "UserId": "a", "ItemType": "Movie", "ItemId": "m1",
                                          "Provider_tmdb": "1"}), {})
    assert status == 200 and body["cleared"] == 1
    played = pwe_get_played(db, ["jf_a", "jf_b"], ["movie"])
    assert played[("tmdb", "1", "movie")] == {"jf_b"}
    db.close()


def test_jellyfin_unplayed_webhook_ignores_other_types(tmpdb):
    import jellyfin_engine as je
    body, status = je.store_webhook(_Req({"NotificationType": "UserDataSaved", "Played": False,
                                          "UserId": "a", "ItemType": "Series", "ItemId": "s1"}), {})
    assert status == 200 and body.get("skipped") == "user_data_saved_not_played"


# ── /api/unwatch routes ───────────────────────────────────────────────
@pytest.fixture
def client(tmp_path, monkeypatch, tmpdb):
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(tmp_path / "config.json"))
    config_store._cfg_cache.update(mtime=None, data=None)
    for k in ("PLEX_TOKEN", "JELLYFIN_API_KEY", "PLEX_WEBHOOK_SECRET", "JELLYFIN_WEBHOOK_SECRET"):
        monkeypatch.delenv(k, raising=False)
    import app as A
    import jellyfin_engine as je
    monkeypatch.setitem(A.app.config, "WTF_CSRF_ENABLED", False)
    monkeypatch.setattr(A.limiter, "enabled", False)
    c = A.app.test_client()
    c.post("/api/setup", json=SETUP_BODY)
    cfg = config_store.load_config()
    cfg["jellyfin_enabled"] = True
    config_store.save_config(cfg)

    calls = []
    monkeypatch.setattr(je, "unwatch_accounts", lambda: {"jf_a": "Alice", "jf_b": "Bob"})
    monkeypatch.setattr(je, "unwatch_items", lambda raw: [("jf_101", "movie", {"Tmdb": "1"})])

    def fake_unmark(raw, account_id, ctx):
        calls.append((raw, account_id))
        return ("failed", "Jellyfin didn't accept the request") if account_id == "jf_b" else ("ok", "")
    monkeypatch.setattr(je, "unmark_on_server", fake_unmark)
    yield A, c, calls
    config_store._cfg_cache.update(mtime=None, data=None)


def test_unwatch_requires_login(client):
    A, _, _ = client
    r = A.app.test_client().post("/api/unwatch/jf_101", json={"userIds": ["jf_a"]})
    assert r.status_code == 401


def test_unwatch_clears_and_reports_server_issues(client, tmpdb):
    A, c, calls = client
    db = _seed(tmpdb)
    r = c.post("/api/unwatch/jf_101", json={"userIds": ["jf_a", "jf_b", "px_9"]})
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is True and data["cleared"] == 3
    assert calls == [("101", "jf_a"), ("101", "jf_b")]
    assert [(i["userId"], i["status"]) for i in data["issues"]] == [("jf_b", "failed")]
    assert pwe_get_played(db, ["jf_a", "jf_b"], ["movie"]).get(("tmdb", "1", "movie"), set()) == set()
    assert pwe_reset_blocks(db, "jf_a", "movie", {"Tmdb": "1"}, "jf_101", "2026-01-01T00:00:00+00:00")
    db.close()


def test_unwatch_rejects_missing_or_foreign_users(client):
    A, c, calls = client
    assert c.post("/api/unwatch/jf_101", json={"userIds": []}).status_code == 400
    assert c.post("/api/unwatch/jf_101", json={}).status_code == 400
    assert c.post("/api/unwatch/jf_101", json={"userIds": ["px_1"]}).status_code == 400
    assert calls == []


def test_unwatch_batch(client, tmpdb):
    A, c, calls = client
    _seed(tmpdb).close()
    r = c.post("/api/unwatch-batch", json={"itemIds": ["jf_101", "jf_102", "bogus"], "userIds": ["jf_a"]})
    assert r.status_code == 200
    assert calls == [("101", "jf_a"), ("102", "jf_a")]
    assert c.post("/api/unwatch-batch", json={"userIds": ["jf_a"]}).status_code == 400
