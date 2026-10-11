"""Security regression tests for the setup and config routes.

Each test points the config at a temporary file, so nothing touches a real
config.json. Background sweeps are switched off before the app is imported.
"""
import os

os.environ.setdefault("RECENTLY_ADDED_SWEEP_INTERVAL", "0")
os.environ.setdefault("PLEX_HISTORY_POLL_INTERVAL", "0")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-for-security-tests")

import pytest

import config_store

SETUP_BODY = {"username": "owner", "password": "ownerpass1", "confirm_password": "ownerpass1",
              "jellyfin_url": "https://jf.example", "jellyfin_api_key": "FAKE_API_KEY_123",
              "webhook_secret": "S" * 20}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(tmp_path / "config.json"))
    config_store._cfg_cache.update(mtime=None, data=None)
    for k in ("PLEX_TOKEN", "JELLYFIN_API_KEY", "PLEX_WEBHOOK_SECRET", "JELLYFIN_WEBHOOK_SECRET"):
        monkeypatch.delenv(k, raising=False)
    import app as A
    monkeypatch.setitem(A.app.config, "WTF_CSRF_ENABLED", False)
    # /api/setup allows 5 calls a minute; these tests run setup more often.
    monkeypatch.setattr(A.limiter, "enabled", False)
    yield A, A.app.test_client()
    config_store._cfg_cache.update(mtime=None, data=None)


# ── H1: setup can't be re-run once an admin exists ────────────────────
def test_setup_open_only_without_admin(client):
    A, c = client
    assert c.get("/api/auth/status").get_json()["needs_setup"] is True
    assert c.post("/api/setup", json=SETUP_BODY).status_code == 200
    assert c.get("/api/auth/status").get_json()["needs_setup"] is False


def test_setup_stays_closed_when_provider_credentials_vanish(client):
    A, c = client
    c.post("/api/setup", json=SETUP_BODY)
    cfg = config_store.load_config()
    cfg.pop("jellyfin_api_key")
    config_store.save_config(cfg)

    attacker = A.app.test_client()
    assert attacker.get("/api/auth/status").get_json()["needs_setup"] is False
    r = attacker.post("/api/setup", json={**SETUP_BODY, "username": "attacker",
                                          "password": "attackpass1", "confirm_password": "attackpass1"})
    assert r.status_code == 400
    assert config_store.get_admin()["username"] == "owner"
    assert attacker.get("/setup").status_code == 302


# ── H2: /api/config never returns secrets ─────────────────────────────
def test_config_response_has_no_secrets(client):
    A, c = client
    c.post("/api/setup", json=SETUP_BODY)
    cfg = c.get("/api/config").get_json()
    for key in ("admin", "plex_token", "jellyfin_api_key", "webhook_secret"):
        assert key not in cfg
    body = c.get("/api/config").get_data(as_text=True)
    assert "FAKE_API_KEY_123" not in body and "password_hash" not in body
    assert cfg["jellyfin_url"] == "https://jf.example"


def test_config_secrets_still_stored(client):
    A, c = client
    c.post("/api/setup", json=SETUP_BODY)
    stored = config_store.load_config()
    assert stored["jellyfin_api_key"] == "FAKE_API_KEY_123"
    assert stored["webhook_secret"] == "S" * 20
    assert stored["admin"]["username"] == "owner"


def test_saving_settings_keeps_secrets(client):
    A, c = client
    c.post("/api/setup", json=SETUP_BODY)
    settings = c.get("/api/config").get_json()
    settings["show_all_libraries"] = False
    assert c.post("/api/config", json=settings).status_code == 200
    stored = config_store.load_config()
    assert stored["jellyfin_api_key"] == "FAKE_API_KEY_123"
    assert stored["admin"]["username"] == "owner"
