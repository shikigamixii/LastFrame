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


# ── M1: sessions can be revoked and expire ────────────────────────────
def _logged_in_client(A, c):
    c.post("/api/setup", json=SETUP_BODY)
    assert c.get("/api/config").status_code == 200
    return c.get_cookie("session").value


def _replay(A, cookie):
    other = A.app.test_client()
    other.set_cookie("session", cookie)
    return other


def test_logout_revokes_copied_cookie(client):
    A, c = client
    cookie = _logged_in_client(A, c)
    c.post("/api/auth/logout")
    assert _replay(A, cookie).get("/api/config").status_code == 401


def test_credential_change_revokes_all_sessions(client):
    A, c = client
    cookie = _logged_in_client(A, c)
    second = A.app.test_client()
    second.post("/api/auth/login", json={"username": "owner", "password": "ownerpass1"})
    assert second.get("/api/config").status_code == 200
    r = c.post("/api/admin/change-credentials", json={
        "current_username": "owner", "current_password": "ownerpass1",
        "new_username": "owner", "new_password": "newpass123", "confirm_password": "newpass123"})
    assert r.get_json()["success"] is True
    assert _replay(A, cookie).get("/api/config").status_code == 401
    assert second.get("/api/config").status_code == 401


def test_cookie_without_session_id_is_rejected(client):
    A, c = client
    _logged_in_client(A, c)
    with c.session_transaction() as s:
        s.pop("sid")                      # a pre-upgrade cookie had no session id
    assert c.get("/api/config").status_code == 401
    assert c.get("/api/auth/status").get_json()["username"] is None


def test_idle_and_absolute_expiry(client, monkeypatch):
    import auth_sessions
    A, c = client
    _logged_in_client(A, c)
    real_time = auth_sessions.time.time
    monkeypatch.setattr(auth_sessions.time, "time", lambda: real_time() + auth_sessions.IDLE_SECONDS + 5)
    assert c.get("/api/config").status_code == 401

    c2 = A.app.test_client()
    monkeypatch.setattr(auth_sessions.time, "time", real_time)
    c2.post("/api/auth/login", json={"username": "owner", "password": "ownerpass1"})
    start = real_time()
    # Active every 6 days, but past the 30-day limit since login.
    for day in range(6, 31, 6):
        monkeypatch.setattr(auth_sessions.time, "time", lambda d=day: start + d * 86400)
        c2.get("/api/config")
    monkeypatch.setattr(auth_sessions.time, "time", lambda: start + auth_sessions.ABSOLUTE_SECONDS + 5)
    assert c2.get("/api/config").status_code == 401


def test_only_session_hash_is_stored(client):
    import auth_sessions
    from db import get_db
    A, c = client
    _logged_in_client(A, c)
    with c.session_transaction() as s:
        sid = s["sid"]
    db = get_db()
    rows = [r["sid_hash"] for r in db.execute("SELECT sid_hash FROM auth_sessions").fetchall()]
    db.close()
    assert sid not in rows and auth_sessions._digest(sid) in rows


# ── M2: webhook secret never reaches the logs ─────────────────────────
def test_webhook_secret_redacted_in_logs(client):
    A, c = client
    line = "POST /api/webhook/plex?secret=SuperSecretValue123&x=1 HTTP/1.1"
    out = A._redact(line)
    assert "SuperSecretValue123" not in out and "secret=[REDACTED]&x=1" in out


def test_gunicorn_access_log_drops_query_string():
    import runpy
    fmt = runpy.run_path(os.path.join(os.path.dirname(__file__), "..", "gunicorn.conf.py"))["access_log_format"]
    assert "%(r)s" not in fmt and "%(q)s" not in fmt and "%(U)s" in fmt


def test_login_and_logout_keep_csrf_token_working(tmp_path, monkeypatch):
    """With CSRF on, the token the page loaded with must keep working across
    login and logout (clearing the whole session would break every POST)."""
    import re as _re
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(tmp_path / "config.json"))
    config_store._cfg_cache.update(mtime=None, data=None)
    import app as A
    monkeypatch.setattr(A.limiter, "enabled", False)
    import bcrypt
    config_store.set_admin("owner", bcrypt.hashpw(b"ownerpass1", bcrypt.gensalt()).decode())
    c = A.app.test_client()
    html = c.get("/").get_data(as_text=True)
    token = _re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    h = {"X-CSRFToken": token}
    assert c.post("/api/auth/login", json={"username": "owner", "password": "ownerpass1"}, headers=h).status_code == 200
    assert c.post("/api/auth/logout", headers=h).status_code == 200
    assert c.post("/api/auth/login", json={"username": "owner", "password": "ownerpass1"}, headers=h).status_code == 200
    config_store._cfg_cache.update(mtime=None, data=None)


# ── M4: an unusable data folder must fail closed, never reopen setup ──
def test_missing_config_means_fresh_install(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(tmp_path / "config.json"))
    config_store._cfg_cache.update(mtime=None, data=None)
    assert config_store.load_config()["show_all_libraries"] is True
    config_store._cfg_cache.update(mtime=None, data=None)


def test_corrupt_config_raises(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(path))
    config_store._cfg_cache.update(mtime=None, data=None)
    with pytest.raises(config_store.ConfigUnreadableError):
        config_store.load_config()
    config_store._cfg_cache.update(mtime=None, data=None)


def test_unreadable_config_raises_and_setup_stays_closed(client, monkeypatch):
    A, c = client
    c.post("/api/setup", json=SETUP_BODY)
    real_stat = os.stat

    def denied(path, *a, **kw):
        if str(path) == config_store.CONFIG_PATH:
            raise PermissionError(13, "Permission denied")
        return real_stat(path, *a, **kw)
    monkeypatch.setattr(config_store.os, "stat", denied)
    config_store._cfg_cache.update(mtime=None, data=None)
    with pytest.raises(config_store.ConfigUnreadableError):
        config_store.load_config()
    attacker = A.app.test_client()
    r = attacker.post("/api/setup", json={**SETUP_BODY, "username": "attacker",
                                          "password": "attackpass1", "confirm_password": "attackpass1"})
    assert r.status_code == 500                      # fails closed, no takeover
    monkeypatch.setattr(config_store.os, "stat", real_stat)
    config_store._cfg_cache.update(mtime=None, data=None)
    assert config_store.get_admin()["username"] == "owner"


def test_startup_check_exits_on_unwritable_data(client, monkeypatch, capsys):
    A, c = client
    # data.db always exists (created at import); pretend it isn't writable.
    assert os.path.exists(os.path.join(config_store.DATA_DIR, "data.db"))
    monkeypatch.setattr(config_store.os, "access", lambda p, mode: not str(p).endswith("data.db"))
    with pytest.raises(SystemExit) as e:
        config_store.check_data_dir()
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "cannot start" in err and "chown" in err


def test_startup_check_passes_when_usable(client):
    config_store.check_data_dir()


# ── L1: new admin passwords need at least 8 characters ────────────────
def test_credential_change_requires_8_char_password(client):
    A, c = client
    c.post("/api/setup", json=SETUP_BODY)
    body = {"current_username": "owner", "current_password": "ownerpass1",
            "new_username": "owner", "new_password": "short", "confirm_password": "short"}
    r = c.post("/api/admin/change-credentials", json=body)
    assert r.status_code == 400 and "8 characters" in r.get_json()["error"]
    assert config_store.verify_admin("owner", "ownerpass1")


# ── L2: the debug sweep can't be triggered by a plain GET/link ────────
def test_debug_managed_sweep_is_post_only(client):
    A, c = client
    c.post("/api/setup", json=SETUP_BODY)
    assert c.get("/api/admin/debug-managed-sweep").status_code == 405


# ── L3: security headers ──────────────────────────────────────────────
def test_csp_on_every_response(client):
    A, c = client
    for path in ("/setup", "/api/auth/status"):
        csp = c.get(path).headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp and "connect-src 'self'" in csp


def test_hsts_only_over_https(client):
    A, c = client
    assert "Strict-Transport-Security" not in c.get("/api/auth/status").headers
    r = c.get("/api/auth/status", base_url="https://localhost")
    assert r.headers.get("Strict-Transport-Security") == "max-age=31536000"


# ── L4: warn once about proxy misconfiguration ────────────────────────
def test_proxy_misconfig_warnings(client, monkeypatch, caplog):
    A, c = client
    monkeypatch.setattr(A, "_proxy_warned", set())
    monkeypatch.setattr(A, "_proxy_hops", 0)
    with caplog.at_level("WARNING"):
        c.get("/api/auth/status", headers={"X-Forwarded-For": "203.0.113.5"})
        c.get("/api/auth/status", headers={"X-Forwarded-For": "203.0.113.6"})
    assert sum("TRUSTED_PROXY_HOPS is 0" in r.getMessage() for r in caplog.records) == 1

    caplog.clear()
    monkeypatch.setattr(A, "_proxy_hops", 1)
    with caplog.at_level("WARNING"):
        c.get("/api/auth/status")
        c.get("/api/auth/status")
    assert sum("without X-Forwarded-For" in r.getMessage() for r in caplog.records) == 1


# ── L5: a non-ASCII webhook secret is rejected cleanly, not a 500 ─────
def test_webhook_non_ascii_secret_rejected(client, monkeypatch):
    A, c = client
    monkeypatch.setenv("JELLYFIN_WEBHOOK_SECRET", "S" * 20)
    assert c.post("/api/webhook/jellyfin?secret=%C3%A9t%C3%A9", json={}).status_code == 401
    assert c.post("/api/webhook/jellyfin", json={}, headers={"X-Webhook-Secret": "wrong"}).status_code == 401


# ── L6: setup status never reveals server URLs ────────────────────────
def test_setup_status_hides_env_urls(client, monkeypatch):
    A, c = client
    monkeypatch.setenv("PLEX_URL", "https://secret-host.plex.direct:32400")
    monkeypatch.setenv("JELLYFIN_URL", "https://jf.secret-host.example")
    r = c.get("/api/auth/status")
    body = r.get_data(as_text=True)
    assert "secret-host" not in body
    d = r.get_json()
    assert d["needs_setup"] is True and d["plex_url_set"] is True and d["jellyfin_url_set"] is True


# ── L7: data files are owner-only ─────────────────────────────────────
def test_data_files_restricted(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store, "DATA_DIR", str(tmp_path))
    loose = tmp_path / "audit.log"
    loose.write_text("x")
    os.chmod(loose, 0o644)
    old_umask = os.umask(0o022)
    try:
        config_store.restrict_data_files()
        assert oct(os.stat(loose).st_mode & 0o777) == "0o600"
        new = tmp_path / "data.db"
        new.write_text("y")
        assert oct(os.stat(new).st_mode & 0o777) == "0o600"
    finally:
        os.umask(old_umask)


# ── L8: the start.sh secret key never goes into the Docker image ──────
def test_dockerignore_excludes_secrets():
    path = os.path.join(os.path.dirname(__file__), "..", ".dockerignore")
    entries = {line.strip() for line in open(path) if line.strip() and not line.startswith("#")}
    for name in (".env", "config.json", "data.db", "audit.log", ".flask_secret_key"):
        assert name in entries
