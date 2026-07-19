#!/usr/bin/env python3
"""Plex Watch History Dashboard with Authentication & Security"""

import json, os, sys, sqlite3, secrets, logging, re, hmac, threading, time
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, jsonify, request, Response, session, abort, redirect, url_for, g
import requests as http_requests
import bcrypt
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, CSRFError

from config_store import (
    APP_DIR, DATA_DIR, CONFIG_PATH, DB_PATH, PAGE_SIZE, PROVIDER_PRIORITY,
    load_config, save_config, get_admin, set_admin, verify_admin,
    update_admin_credentials, is_setup_needed,
)
from db import get_db, init_db
from plex_api import (
    PLEX_HEADERS, get_plex_url, get_plex_token, get_webhook_secret,
    plex_get, plex_delete, plex_get_raw, plex_get_with_token,
    plex_accounts, plex_all_accounts,
    plex_owner_id, _local_account_id, plex_sections, plex_genre_id,
    plex_tv_home_users, plex_tv_switch_token, plex_tv_switch_token_diag,
    plex_refresh_access_tokens,
    ts_to_iso, parse_plex_guids, get_item_providers,
    set_request_hook as _plex_set_request_hook,
)
from assignments import (
    get_assignment_by_provider, set_assignment_by_provider, delete_assignment_by_provider,
    get_assignment_by_item_id, set_assignment_by_item_id, delete_assignment_by_item_id,
    get_assigned_ids, _load_assignment_maps, _resolve_target,
)
from webhook_state import (
    pwe_has_data, pwe_get_played, pwe_get_played_with_ts, pwe_get_played_by_ratingkey,
    pwe_resolve_rating_keys, is_item_watched_pwe, get_last_played_pwe,
)
from auto_delete import (
    is_auto_delete_active, _get_enabled_since, _auto_delete_candidate,
    _maybe_auto_delete, _run_sweep, _auto_delete_sweep,
)
import recently_added

app = Flask(__name__)

def _static_version():
    static_dir = os.path.join(app.root_path, "static")
    try:
        mtimes = [os.path.getmtime(os.path.join(static_dir, f))
                  for f in os.listdir(static_dir)
                  if os.path.isfile(os.path.join(static_dir, f))]
        return str(int(max(mtimes))) if mtimes else "0"
    except OSError:
        return "0"

STATIC_VERSION = _static_version()

@app.context_processor
def _inject_static_version():
    return {"static_v": STATIC_VERSION}

# Only trust X-Forwarded-* headers when a reverse proxy is actually in front.
# On the documented direct-HTTP deployment there is no proxy, so blindly
# trusting these headers would let any client spoof their source IP (by sending
# their own X-Forwarded-For) and thereby bypass the per-IP login rate limiter
# and forge audit-log entries. Default off; set TRUSTED_PROXY_HOPS to the
# number of proxy hops (usually 1) when serving behind nginx/Caddy/Traefik.
_proxy_hops = int(os.environ.get("TRUSTED_PROXY_HOPS", "0") or "0")
if _proxy_hops > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_proxy_hops, x_proto=_proxy_hops,
                            x_host=_proxy_hops, x_prefix=_proxy_hops)

app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
# Default off so plain-HTTP LAN deployments (the documented happy path) can
# persist the session cookie that Flask-WTF needs to validate CSRF tokens.
# Set SESSION_COOKIE_SECURE=1 when serving over HTTPS.
_secure_cookie = os.environ.get("SESSION_COOKIE_SECURE", "0").strip().lower() in ("1", "true", "yes", "on")
app.config.update(
    SESSION_COOKIE_SECURE=_secure_cookie,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    WTF_CSRF_TIME_LIMIT=None,
)

csrf = CSRFProtect(app)

@app.errorhandler(CSRFError)
def _csrf_error(e):
    return jsonify({"error": "CSRF token missing or invalid", "csrf_failed": True}), 400

@app.after_request
def _security_headers(resp):
    # Cheap baseline hardening. DENY framing (the UI has one-click delete
    # buttons, so clickjacking matters); stop MIME sniffing; trim referrers.
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["2000 per day", "500 per hour"],
    storage_uri="memory://",
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(DATA_DIR, 'audit.log')),
    ],
)

PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
PLEX_WEBHOOK_SECRET = os.environ.get("PLEX_WEBHOOK_SECRET", "")
PORT = int(os.environ.get("PORT", "8080"))

# Run DB init at import time so it works under gunicorn/wsgi as well as direct run
init_db()

# Kick off the auto-delete sweep timer at import time. Runs once per process —
# gunicorn is configured with a single worker to keep this from racing on SQLite.
_auto_delete_sweep()

# Discovery sweep for the Recently Added list. Records newly-added titles so the
# list is populated without manual action. Set RECENTLY_ADDED_SWEEP_INTERVAL=0
# to disable. First run is delayed so init_db and Plex config settle first.
RECENTLY_ADDED_SWEEP_INTERVAL = int(os.environ.get("RECENTLY_ADDED_SWEEP_INTERVAL", "1800"))

def _recently_added_sweep():
    try:
        if get_plex_token() and get_plex_url():
            added = recently_added.discover()
            if added:
                app.logger.info(f"Recently-added sweep: recorded {added} new title(s)")
    except Exception as e:
        app.logger.warning(f"Recently-added sweep error: {e}")
    finally:
        if RECENTLY_ADDED_SWEEP_INTERVAL > 0:
            t = threading.Timer(RECENTLY_ADDED_SWEEP_INTERVAL, _recently_added_sweep)
            t.daemon = True
            t.start()

if RECENTLY_ADDED_SWEEP_INTERVAL > 0:
    _ra_t = threading.Timer(20, _recently_added_sweep)
    _ra_t.daemon = True
    _ra_t.start()

# Surface unhandled exceptions in `docker compose logs` (audit.log already
# captures them via Flask's logger, but stderr is what the container shows).
@app.errorhandler(Exception)
def _log_unhandled(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    import traceback, sys
    print(f"[500] {request.method} {request.path}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    traceback.print_exc(file=sys.stderr)
    return jsonify({"error": "Internal server error", "type": type(e).__name__, "message": str(e)}), 500

# ── Perf diagnostics ──────────────────────────────────────────────────
# Per-request timing for the endpoints the home page calls on load.
# Each request emits one stderr line so it shows up in `docker compose logs`.
# Format: [PERF] /api/path 200 total=Xms plex_calls=N plex_total=Yms top=path(Nx=Yms), ...
_PERF_PATHS = {
    "/api/users", "/api/libraries",
    "/api/recent/movies", "/api/recent/episodes",
    "/api/activity", "/api/events/last-update",
    "/api/watch-summary/items",
}

def _perf_record_plex_call(path, dur):
    try:
        calls = getattr(g, "_plex_calls", None)
    except RuntimeError:
        return
    if calls is not None:
        calls.append((path, dur))

_plex_set_request_hook(_perf_record_plex_call)

@app.before_request
def _perf_start():
    if request.path in _PERF_PATHS:
        g._plex_calls = []
        g._req_t0 = time.monotonic()

@app.after_request
def _perf_log(resp):
    t0 = getattr(g, "_req_t0", None)
    if t0 is None:
        return resp
    calls = getattr(g, "_plex_calls", []) or []
    total_ms = int((time.monotonic() - t0) * 1000)
    plex_total_ms = int(sum(d for _, d in calls) * 1000)
    agg = {}
    for p, d in calls:
        slot = agg.setdefault(p, [0, 0.0])
        slot[0] += 1
        slot[1] += d
    parts = sorted(agg.items(), key=lambda x: -x[1][1])[:5]
    breakdown = ", ".join(f"{p}({n}x={int(t*1000)}ms)" for p, (n, t) in parts) or "-"
    print(f"[PERF] {request.path} {resp.status_code} total={total_ms}ms "
          f"plex_calls={len(calls)} plex_total={plex_total_ms}ms top={breakdown}",
          file=sys.stderr, flush=True)
    return resp

# ── Auth helpers ──────────────────────────────────────────────────────
def is_admin_logged_in():
    return session.get("logged_in") == True

def login_required_api(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_admin_logged_in():
            return jsonify({"error": "Unauthorized", "login_required": True}), 401
        return f(*args, **kwargs)
    return decorated

_ID_RE = re.compile(r'^\d{1,20}$')
_USERNAME_RE = re.compile(r'^[a-zA-Z0-9_\-\.@]+$')
def vid(v):
    """Validate a numeric ID from a URL path; abort 400 if invalid."""
    if not v or not _ID_RE.match(str(v)):
        abort(400)
    return str(v)

# ── Routes ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if is_setup_needed():
        return redirect(url_for("setup_page"))
    return render_template("index.html")

@app.route("/setup")
def setup_page():
    if not is_setup_needed():
        return redirect(url_for("index"))
    return render_template("setup.html")

@app.route("/api/auth/status")
def auth_status():
    resp = {"logged_in": is_admin_logged_in(), "username": session.get("username"),
            "needs_setup": is_setup_needed()}
    if resp["needs_setup"]:
        resp["plex_url_from_env"] = os.environ.get("PLEX_URL", "")
        resp["plex_token_set"] = bool(os.environ.get("PLEX_TOKEN"))
        resp["webhook_secret_set"] = bool(os.environ.get("PLEX_WEBHOOK_SECRET"))
    return jsonify(resp)

@app.route("/api/setup", methods=["POST"])
@limiter.limit("5 per minute")
def api_setup():
    if not is_setup_needed():
        return jsonify({"success": False, "error": "Setup already complete"}), 400
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password", "")
    confirm = data.get("confirm_password", "")
    plex_url = (data.get("plex_url") or "").strip()
    plex_token = (data.get("plex_token") or "").strip()
    webhook_secret = (data.get("webhook_secret") or "").strip()
    if not username:
        return jsonify({"success": False, "error": "Username is required"}), 400
    if len(username) > 50:
        return jsonify({"success": False, "error": "Username must be 50 characters or fewer"}), 400
    if not _USERNAME_RE.match(username):
        return jsonify({"success": False, "error": "Username may only contain letters, numbers, and _ - . @"}), 400
    if len(password) < 8:
        return jsonify({"success": False, "error": "Password must be at least 8 characters"}), 400
    if password != confirm:
        return jsonify({"success": False, "error": "Passwords do not match"}), 400
    if not os.environ.get("PLEX_TOKEN") and not plex_token:
        return jsonify({"success": False, "error": "Plex token is required"}), 400
    if not os.environ.get("PLEX_WEBHOOK_SECRET") and not webhook_secret:
        return jsonify({"success": False, "error": "Webhook secret is required"}), 400
    if webhook_secret and len(webhook_secret) < 16:
        return jsonify({"success": False, "error": "Webhook secret must be at least 16 characters"}), 400
    salt = bcrypt.gensalt()
    pwd_hash = bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")
    cfg = load_config()
    cfg["admin"] = {"username": username, "password_hash": pwd_hash}
    if plex_url:
        cfg["plex_url"] = plex_url
    if plex_token:
        cfg["plex_token"] = plex_token
    if webhook_secret:
        cfg["webhook_secret"] = webhook_secret
    save_config(cfg)
    session["logged_in"] = True
    session["username"] = username
    safe_u = re.sub(r'[\x00-\x1f\x7f]', '', username)
    app.logger.info(f"Initial setup: admin '{safe_u}' created from {request.remote_addr}")
    return jsonify({"success": True})

@app.route("/api/auth/login", methods=["POST"])
@limiter.limit("5 per minute")
def api_login():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "")
    password = data.get("password", "")
    # Strip control chars before logging so a newline-laced username can't forge
    # extra audit-log lines (matches the sanitization used in api_setup).
    safe_u = re.sub(r'[\x00-\x1f\x7f]', '', username or "")
    if verify_admin(username, password):
        session["logged_in"] = True
        session["username"] = username
        app.logger.info(f"Successful login for '{safe_u}' from {request.remote_addr}")
        return jsonify({"success": True})
    app.logger.warning(f"Failed login attempt for '{safe_u}' from {request.remote_addr}")
    return jsonify({"success": False, "error": "Invalid credentials"}), 401

@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.pop("logged_in", None); session.pop("username", None)
    return jsonify({"success": True})

@app.route("/api/admin/change-credentials", methods=["POST"])
@limiter.limit("5 per minute")
@login_required_api
def api_change_credentials():
    data = request.json
    current_username = data.get("current_username")
    current_password = data.get("current_password")
    new_username = data.get("new_username")
    new_password = data.get("new_password")
    confirm_password = data.get("confirm_password")
    if not current_username or not current_password:
        return jsonify({"success": False, "error": "Current username and password required"}), 400
    if not new_username or not new_password:
        return jsonify({"success": False, "error": "New username and password required"}), 400
    new_username = new_username.strip()
    if len(new_username) > 50:
        return jsonify({"success": False, "error": "Username must be 50 characters or fewer"}), 400
    if not _USERNAME_RE.match(new_username):
        return jsonify({"success": False, "error": "Username may only contain letters, numbers, and _ - . @"}), 400
    if new_password != confirm_password:
        return jsonify({"success": False, "error": "New passwords do not match"}), 400
    success, msg = update_admin_credentials(current_username, current_password, new_username, new_password)
    if success:
        safe_old = re.sub(r'[\x00-\x1f\x7f]', '', current_username)
        safe_new = re.sub(r'[\x00-\x1f\x7f]', '', new_username)
        app.logger.info(f"Admin credentials changed from {safe_old} to {safe_new} from {request.remote_addr}")
        session.pop("logged_in", None); session.pop("username", None)
        return jsonify({"success": True, "message": msg})
    return jsonify({"success": False, "error": msg}), 400

@app.route("/api/config")
@login_required_api
def api_get_config(): return jsonify(load_config())

@app.route("/api/config", methods=["POST"])
@login_required_api
def api_save_config():
    cfg = load_config()
    incoming = request.json or {}
    cfg["monitored_libraries"] = incoming.get("monitored_libraries", cfg.get("monitored_libraries", []))
    cfg["show_all_libraries"] = incoming.get("show_all_libraries", cfg.get("show_all_libraries", True))
    if "auto_delete_enabled" in incoming:
        cfg["auto_delete_enabled"] = bool(incoming["auto_delete_enabled"])
    if "auto_delete_libraries" in incoming:
        new_ids = [str(x) for x in (incoming["auto_delete_libraries"] or [])]
        old_ids = set(str(x) for x in (cfg.get("auto_delete_libraries") or []))
        enabled_at_map = dict(cfg.get("auto_delete_library_enabled_at") or {})
        now_ts = datetime.now(timezone.utc).isoformat()
        for lid in new_ids:
            if lid not in old_ids:
                enabled_at_map[lid] = now_ts
        for lid in list(enabled_at_map.keys()):
            if lid not in new_ids:
                del enabled_at_map[lid]
        cfg["auto_delete_libraries"] = new_ids
        cfg["auto_delete_library_enabled_at"] = enabled_at_map
    if "auto_delete_grace_days" in incoming:
        cfg["auto_delete_grace_days"] = max(0, int(incoming["auto_delete_grace_days"] or 0))
    if "auto_delete_min_delay_minutes" in incoming:
        cfg["auto_delete_min_delay_minutes"] = max(0, int(incoming["auto_delete_min_delay_minutes"] or 0))
    if "timezone" in incoming:
        cfg["timezone"] = (incoming["timezone"] or "").strip()
    if "recently_added_window_days" in incoming:
        try:
            days = int(incoming["recently_added_window_days"])
        except (TypeError, ValueError):
            days = recently_added.DEFAULT_WINDOW_DAYS
        cfg["recently_added_window_days"] = max(1, days)
    save_config(cfg)
    return jsonify({"success": True})

@app.route("/api/webhook/plex", methods=["POST"])
@csrf.exempt
@limiter.limit("120 per minute")
def api_webhook_plex():
    secret = get_webhook_secret()
    if not secret:
        # Webhook is disabled until a secret is configured.
        return jsonify({"error": "Webhook not configured"}), 503
    incoming = request.headers.get("X-Webhook-Secret") or request.args.get("secret", "")
    if not hmac.compare_digest(incoming, secret):
        return jsonify({"error": "Unauthorized"}), 401

    # Plex sends multipart/form-data with a 'payload' JSON field
    payload_str = request.form.get("payload")
    if payload_str:
        try: data = json.loads(payload_str)
        except Exception: return jsonify({"error": "invalid payload"}), 400
    else:
        data = request.get_json(silent=True) or {}

    raw_event = (data.get("event") or "").lower()
    if raw_event == "media.scrobble":
        event_type = "play"
    else:
        return jsonify({"ok": True, "skipped": raw_event})

    account = data.get("Account") or {}
    account_id = account.get("id")
    if account_id is None: return jsonify({"error": "no account"}), 400
    account_id = _local_account_id(str(account_id), account.get("title", ""))

    meta = data.get("Metadata") or {}
    raw_type = (meta.get("type") or "").lower()
    type_map = {"movie": "movie", "episode": "episode", "show": "show"}
    item_type = type_map.get(raw_type)
    if not item_type:
        return jsonify({"ok": True, "skipped": f"type:{raw_type}"})

    providers = parse_plex_guids(meta.get("Guid") or [])
    rating_key = str(meta.get("ratingKey") or "")
    if not providers and not rating_key:
        return jsonify({"ok": True, "skipped": "no provider ids or rating_key"})

    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    stored = 0
    for ptype, pid in providers.items():
        db.execute("""INSERT INTO plex_watch_events (plex_account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plex_account_id, provider_type, provider_id, item_type)
            DO UPDATE SET event_type=excluded.event_type, updated_at=excluded.updated_at, rating_key=excluded.rating_key""",
            (account_id, ptype.lower(), str(pid), item_type, event_type, now, rating_key))
        stored += 1
    if stored == 0 and rating_key:
        # No provider GUIDs in payload — store by rating_key as fallback so badge detection and auto-delete still work
        db.execute("""INSERT INTO plex_watch_events (plex_account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key)
            VALUES (?, 'ratingkey', ?, ?, ?, ?, ?)
            ON CONFLICT(plex_account_id, provider_type, provider_id, item_type)
            DO UPDATE SET event_type=excluded.event_type, updated_at=excluded.updated_at, rating_key=excluded.rating_key""",
            (account_id, rating_key, item_type, event_type, now, rating_key))
        stored = 1
    db.commit()
    app.logger.info(f"Plex webhook: account={account_id} ({account.get('title','')}) event={raw_event} type={item_type} providers={providers} rating_key={rating_key}")
    cfg = load_config()
    if event_type == "play" and item_type in ("movie", "episode"):
        try:
            _maybe_auto_delete(db, cfg, rating_key, item_type, providers, meta)
        except Exception as e:
            app.logger.warning(f"Auto-delete check error for {rating_key}: {e}")
    db.close()
    return jsonify({"ok": True, "stored": stored})

@app.route("/api/webhook-events/status")
@login_required_api
def api_webhook_events_status():
    db = get_db()
    event_count = db.execute("SELECT COUNT(*) as cnt FROM plex_watch_events").fetchone()["cnt"]
    play_count = db.execute("SELECT COUNT(*) as cnt FROM plex_watch_events WHERE event_type='play'").fetchone()["cnt"]
    accounts = db.execute("SELECT plex_account_id, COUNT(*) as cnt FROM plex_watch_events WHERE event_type='play' GROUP BY plex_account_id ORDER BY plex_account_id").fetchall()
    last_row = db.execute("SELECT updated_at FROM plex_watch_events ORDER BY updated_at DESC LIMIT 1").fetchone()
    db.close()
    return jsonify({
        "total_events": event_count,
        "play_events": play_count,
        "last_event_at": last_row["updated_at"] if last_row else None,
        "accounts": [{"plex_account_id": r["plex_account_id"], "play_events": r["cnt"]} for r in accounts]
    })

@app.route("/api/webhook-events/for-item/<rating_key>")
@login_required_api
def api_webhook_events_for_item(rating_key):
    rating_key = vid(rating_key)
    db = get_db()
    rows = db.execute(
        "SELECT plex_account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key "
        "FROM plex_watch_events WHERE rating_key=? ORDER BY updated_at DESC",
        (rating_key,)).fetchall()
    recent = db.execute(
        "SELECT plex_account_id, item_type, rating_key, updated_at FROM plex_watch_events "
        "ORDER BY updated_at DESC LIMIT 20").fetchall()
    db.close()
    name_by_id = {a["id"]: a["name"] for a in plex_accounts()}
    return jsonify({
        "rating_key": rating_key,
        "events_for_item": [
            {"plex_account_id": r["plex_account_id"], "account_name": name_by_id.get(r["plex_account_id"], "?"),
             "provider_type": r["provider_type"], "provider_id": r["provider_id"],
             "item_type": r["item_type"], "event_type": r["event_type"],
             "updated_at": r["updated_at"], "rating_key": r["rating_key"]}
            for r in rows
        ],
        "most_recent_overall": [
            {"plex_account_id": r["plex_account_id"], "account_name": name_by_id.get(r["plex_account_id"], "?"),
             "item_type": r["item_type"], "rating_key": r["rating_key"], "updated_at": r["updated_at"]}
            for r in recent
        ]
    })

@app.route("/api/webhook-events", methods=["DELETE"])
@login_required_api
def api_webhook_events_clear():
    db = get_db()
    db.execute("DELETE FROM plex_watch_events")
    db.commit(); db.close()
    return jsonify({"success": True})

@app.route("/api/item/<item_id>")
@login_required_api
def api_item_info(item_id):
    item_id = vid(item_id)
    mc = plex_get(f"/library/metadata/{item_id}")
    items = mc.get("Metadata") or mc.get("Directory") or []
    if not items: return jsonify({"error": "not found"}), 404
    i = items[0]
    plex_type = i.get("type", "")
    type_map = {"show": ("tvshows", "Series"), "movie": ("movies", "Movie"),
                "season": ("tvshows", "Season"), "episode": ("tvshows", "Episode")}
    ctype, itype = type_map.get(plex_type, ("", plex_type))
    return jsonify({
        "id": str(i.get("ratingKey", item_id)),
        "name": i.get("title", ""),
        "type": itype,
        "collectionType": ctype,
        "seriesId": str(i["grandparentRatingKey"]) if i.get("grandparentRatingKey") else None,
        "seriesName": i.get("grandparentTitle") or None
    })

@app.route("/api/users")
@login_required_api
def api_users(): return jsonify(plex_accounts())

@app.route("/api/libraries")
@login_required_api
def api_libraries():
    sections = plex_sections()
    config = load_config()
    out = []
    for s in sections:
        if s.get("type") not in ("show", "movie"): continue
        lid = str(s["key"])
        ltype = "tvshows" if s["type"] == "show" else "movies"
        out.append({"id": lid, "name": s["title"], "type": ltype,
            "monitored": config.get("show_all_libraries", True) or lid in config.get("monitored_libraries", [])})
    return jsonify(out)

@app.route("/api/series")
@login_required_api
def api_series():
    pid = request.args.get("parentId", "")
    search = request.args.get("search", "")
    try: page = max(1, int(request.args.get("page", "1")))
    except (ValueError, TypeError): page = 1
    ids = request.args.get("ids", "")
    genre = request.args.get("genre", "")
    if pid and not _ID_RE.match(pid): abort(400)

    if ids:
        id_list = [i.strip() for i in ids.split(",") if i.strip() and _ID_RE.match(i.strip())]
        items = []
        for rid in id_list:
            try:
                mc = plex_get(f"/library/metadata/{rid}")
                m = (mc.get("Metadata") or [])[0]
                items.append({"id": str(m["ratingKey"]), "name": m.get("title", ""), "year": m.get("year")})
            except Exception: continue
        return jsonify({"items": items, "totalCount": len(items), "page": 1, "pageSize": len(items), "isSearch": False})

    if not pid:
        return jsonify({"items": [], "totalCount": 0, "page": page, "pageSize": PAGE_SIZE, "isSearch": False})

    params = {"type": 2, "sort": "titleSort:asc",
              "X-Plex-Container-Size": 100 if search else PAGE_SIZE,
              "X-Plex-Container-Start": 0 if search else (page - 1) * PAGE_SIZE}
    if search: params["title"] = search
    if genre:
        gid = plex_genre_id(pid, genre)
        if gid: params["genre"] = gid
    mc = plex_get(f"/library/sections/{pid}/all", params)
    items = mc.get("Metadata", [])
    total = mc.get("totalSize", mc.get("size", len(items)))
    return jsonify({"items": [{"id": str(i["ratingKey"]), "name": i.get("title", ""), "year": i.get("year")} for i in items],
        "totalCount": total, "page": page, "pageSize": PAGE_SIZE, "isSearch": bool(search)})

@app.route("/api/movies")
@login_required_api
def api_movies():
    pid = request.args.get("parentId", "")
    search = request.args.get("search", "")
    try: page = max(1, int(request.args.get("page", "1")))
    except (ValueError, TypeError): page = 1
    ids = request.args.get("ids", "")
    genre = request.args.get("genre", "")
    if pid and not _ID_RE.match(pid): abort(400)

    if ids:
        id_list = [i.strip() for i in ids.split(",") if i.strip() and _ID_RE.match(i.strip())]
        items = []
        for rid in id_list:
            try:
                mc = plex_get(f"/library/metadata/{rid}")
                m = (mc.get("Metadata") or [])[0]
                items.append({"id": str(m["ratingKey"]), "name": m.get("title", ""), "year": m.get("year")})
            except Exception: continue
        return jsonify({"items": items, "totalCount": len(items), "page": 1, "pageSize": len(items), "isSearch": False})

    if not pid:
        return jsonify({"items": [], "totalCount": 0, "page": page, "pageSize": PAGE_SIZE, "isSearch": False})

    params = {"type": 1, "sort": "titleSort:asc",
              "X-Plex-Container-Size": 100 if search else PAGE_SIZE,
              "X-Plex-Container-Start": 0 if search else (page - 1) * PAGE_SIZE}
    if search: params["title"] = search
    if genre:
        gid = plex_genre_id(pid, genre)
        if gid: params["genre"] = gid
    mc = plex_get(f"/library/sections/{pid}/all", params)
    items = mc.get("Metadata", [])
    total = mc.get("totalSize", mc.get("size", len(items)))
    return jsonify({"items": [{"id": str(i["ratingKey"]), "name": i.get("title", ""), "year": i.get("year")} for i in items],
        "totalCount": total, "page": page, "pageSize": PAGE_SIZE, "isSearch": bool(search)})

@app.route("/api/search")
@login_required_api
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"items": [], "query": ""})

    config = load_config()
    show_all = config.get("show_all_libraries", True)
    monitored = set(config.get("monitored_libraries", []))

    results = []
    for s in plex_sections():
        stype = s.get("type")
        if stype not in ("show", "movie"): continue
        lid = str(s["key"])
        if not show_all and lid not in monitored: continue
        plex_type = 1 if stype == "movie" else 2
        kind = "movie" if stype == "movie" else "series"
        try:
            mc = plex_get(f"/library/sections/{lid}/all", {
                "type": plex_type, "title": q, "sort": "titleSort:asc",
                "X-Plex-Container-Size": 25, "X-Plex-Container-Start": 0,
            })
        except Exception:
            continue
        for i in mc.get("Metadata", []):
            results.append({"id": str(i["ratingKey"]), "name": i.get("title", ""),
                            "year": i.get("year"),
                            "type": kind, "libId": lid})

    qlow = q.lower()
    results.sort(key=lambda x: (0 if (x["name"] or "").lower().startswith(qlow) else 1,
                                (x["name"] or "").lower()))
    return jsonify({"items": results, "query": q})

@app.route("/api/watch-summary")
@login_required_api
def api_watch_summary():
    pid = request.args.get("parentId", "")
    item_type = request.args.get("type", "movies")
    plex_type = 1 if item_type == "movies" else 2
    ws_item_type = "movie" if item_type == "movies" else "show"
    if not pid: return jsonify({})
    if not _ID_RE.match(pid): abort(400)

    accounts = plex_accounts()
    if not accounts: return jsonify({})
    all_account_ids = {a["id"] for a in accounts}

    mc = plex_get(f"/library/sections/{pid}/all",
                  {"type": plex_type, "X-Plex-Container-Size": 10000,
                   "X-Plex-Container-Start": 0, "includeGuids": 1})
    all_items = mc.get("Metadata", [])
    if not all_items: return jsonify({})

    db = get_db()
    item_assignments, provider_assignments = _load_assignment_maps(db)

    def resolve_target(providers, item_id):
        return _resolve_target(providers, item_id, item_assignments, provider_assignments, all_account_ids)

    has_pwe = pwe_has_data(db)

    result = {}

    if has_pwe:
        owner_id = plex_owner_id()

        if item_type == "movies":
            played = pwe_get_played(db, list(all_account_ids), ["movie"])
            played_rk = pwe_get_played_by_ratingkey(db, list(all_account_ids), ["movie"])
            for i in all_items:
                iid = str(i["ratingKey"])
                providers = parse_plex_guids(i.get("Guid", []))
                target_ids = resolve_target(providers, iid)
                owner_saw_it = (i.get("viewCount") or 0) > 0
                if not target_ids:
                    result[iid] = {"watched": 0, "total": 0, "watcher_ids": []}
                    continue
                watched_count = 0; watcher_ids = []
                rk_watchers = played_rk.get(iid, set())
                for aid in target_ids:
                    saw_it = (aid == owner_id and owner_saw_it) or (
                        bool(providers) and is_item_watched_pwe(providers, "movie", {aid}, played)) or (
                        aid in rk_watchers)
                    if saw_it:
                        watched_count += 1; watcher_ids.append(aid)
                result[iid] = {"watched": watched_count, "total": len(target_ids), "watcher_ids": watcher_ids}
        else:
            # TV shows: aggregate episode-level webhook events
            ep_mc = plex_get(f"/library/sections/{pid}/all",
                             {"type": 4, "X-Plex-Container-Size": 50000,
                              "X-Plex-Container-Start": 0, "includeGuids": 1})
            show_eps = {}  # {show_id: [(providers, ep_rk), ...]}
            for ep in ep_mc.get("Metadata", []):
                show_id = str(ep.get("grandparentRatingKey", ""))
                ep_rk = str(ep.get("ratingKey", ""))
                providers = parse_plex_guids(ep.get("Guid", []))
                if show_id and (providers or ep_rk):
                    show_eps.setdefault(show_id, []).append((providers, ep_rk))
            ep_played = pwe_get_played(db, list(all_account_ids), ["episode"])
            ep_played_rk = pwe_get_played_by_ratingkey(db, list(all_account_ids), ["episode"])

            for i in all_items:
                iid = str(i["ratingKey"])
                show_providers = parse_plex_guids(i.get("Guid", []))
                target_ids = resolve_target(show_providers, iid)
                leaf = i.get("leafCount") or 0
                viewed = i.get("viewedLeafCount") or 0
                owner_saw_all = leaf > 0 and viewed >= leaf
                eps = show_eps.get(iid, [])
                if not target_ids or not eps:
                    result[iid] = {"watched": 0, "total": len(target_ids), "watcher_ids": []}
                    continue
                watched_count = 0; watcher_ids = []
                for aid in target_ids:
                    if aid == owner_id and owner_saw_all:
                        saw_all = True
                    else:
                        saw_all = all(
                            (bool(p) and is_item_watched_pwe(p, "episode", {aid}, ep_played)) or
                            (rk and aid in ep_played_rk.get(rk, set()))
                            for p, rk in eps
                        )
                    if saw_all:
                        watched_count += 1; watcher_ids.append(aid)
                result[iid] = {"watched": watched_count, "total": len(target_ids), "watcher_ids": watcher_ids}
    else:
        # No webhook data: only owner's viewCount is reliable.
        owner_id = plex_owner_id()
        owner_watched = set()
        for i in all_items:
            rid = str(i["ratingKey"])
            if item_type == "movies":
                if (i.get("viewCount") or 0) > 0: owner_watched.add(rid)
            else:
                leaf = i.get("leafCount") or 0
                viewed = i.get("viewedLeafCount") or 0
                if leaf > 0 and viewed >= leaf: owner_watched.add(rid)

        for i in all_items:
            iid = str(i["ratingKey"])
            item_providers = parse_plex_guids(i.get("Guid", []))
            target_ids = resolve_target(item_providers, iid)
            owner_watched_it = iid in owner_watched and owner_id in target_ids
            wids = [owner_id] if owner_watched_it else []
            result[iid] = {"watched": len(wids), "total": len(target_ids), "watcher_ids": wids}

    db.close()
    return jsonify(result)

@app.route("/api/genres")
@login_required_api
def api_genres():
    pid = request.args.get("parentId", "")
    if not pid: return jsonify([])
    if not _ID_RE.match(pid): abort(400)
    try:
        mc = plex_get(f"/library/sections/{pid}/genre")
        genres = sorted(set((g.get("title") or g.get("tag") or "")
                            for g in mc.get("Directory", [])
                            if (g.get("title") or g.get("tag"))))
        return jsonify(genres)
    except Exception:
        app.logger.exception("Failed to fetch genres")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/seasons/<series_id>")
@login_required_api
def api_seasons(series_id):
    series_id = vid(series_id)
    mc = plex_get(f"/library/metadata/{series_id}/children")
    seasons_raw = [s for s in mc.get("Metadata", []) if s.get("type") == "season"]
    if not seasons_raw: return jsonify([])

    accounts = plex_accounts()
    assigned_ids = get_assigned_ids(series_id)
    target_accounts = accounts if assigned_ids is None else [a for a in accounts if a["id"] in assigned_ids]
    if not target_accounts: target_accounts = accounts

    db = get_db()
    has_pwe = pwe_has_data(db)

    if has_pwe:
        target_ids = [a["id"] for a in target_accounts]
        played = pwe_get_played(db, target_ids, ["episode"])
        db.close()
        owner_id = plex_owner_id()

        try:
            leaf_mc = plex_get(f"/library/metadata/{series_id}/allLeaves", {"includeGuids": 1})
            all_eps = leaf_mc.get("Metadata", [])
        except Exception:
            all_eps = []

        season_eps = {}
        for ep in all_eps:
            sk = str(ep.get("parentRatingKey", ""))
            if sk:
                season_eps.setdefault(sk, []).append(
                    (str(ep["ratingKey"]), parse_plex_guids(ep.get("Guid", []))))

        result = []
        for s in seasons_raw:
            sid = str(s["ratingKey"])
            base_total = s.get("leafCount") or 0
            owner_viewed = s.get("viewedLeafCount") or 0
            eps = season_eps.get(sid, [])
            completed_users, per_user = 0, []
            for acc in target_accounts:
                if acc["id"] == owner_id and base_total > 0 and owner_viewed >= base_total:
                    played_count, total, completed = owner_viewed, base_total, True
                elif eps:
                    total = len(eps)
                    played_count = sum(
                        1 for _, providers in eps
                        if providers and is_item_watched_pwe(providers, "episode", {acc["id"]}, played))
                    completed = total > 0 and played_count >= total
                else:
                    played_count = 0
                    total = base_total
                    completed = False
                if completed: completed_users += 1
                per_user.append({"userId": acc["id"], "userName": acc["name"],
                                 "playedCount": played_count, "totalCount": total, "completed": completed})
            result.append({"id": sid, "name": s.get("title", ""), "indexNumber": s.get("index", 0),
                           "totalEpisodes": base_total, "userProgress": per_user,
                           "completedUsers": completed_users, "totalAssignedUsers": len(target_accounts)})
        return jsonify(result)

    db.close()
    # No webhook data: only owner's viewedLeafCount is reliable.
    owner_id = plex_owner_id()
    result = []
    for s in seasons_raw:
        sid = str(s["ratingKey"])
        base_total = s.get("leafCount") or 0
        owner_played = s.get("viewedLeafCount") or 0
        completed_users, per_user = 0, []
        for acc in target_accounts:
            if acc["id"] == owner_id:
                played, total = owner_played, base_total
            else:
                played, total = 0, base_total
            completed = total > 0 and played >= total
            if completed: completed_users += 1
            per_user.append({"userId": acc["id"], "userName": acc["name"],
                             "playedCount": played, "totalCount": total, "completed": completed})
        result.append({"id": sid, "name": s.get("title", ""), "indexNumber": s.get("index", 0),
                       "totalEpisodes": base_total, "userProgress": per_user,
                       "completedUsers": completed_users, "totalAssignedUsers": len(target_accounts)})
    return jsonify(result)

@app.route("/api/season-watch-status/<series_id>/<season_id>")
@login_required_api
def api_season_watch_status(series_id, season_id):
    series_id = vid(series_id); season_id = vid(season_id)
    accounts = plex_accounts()

    db = get_db()
    has_pwe = pwe_has_data(db)

    if has_pwe:
        owner_id = plex_owner_id()
        all_ids = [a["id"] for a in accounts]
        played = pwe_get_played(db, all_ids, ["episode"])
        played_ts = pwe_get_played_with_ts(db, all_ids, ["episode"])
        db.close()

        mc = plex_get(f"/library/metadata/{season_id}/children", {"includeGuids": 1})
        episodes = [ep for ep in mc.get("Metadata", []) if ep.get("type") == "episode"]
        result = []
        for ep in episodes:
            eid = str(ep["ratingKey"])
            providers = parse_plex_guids(ep.get("Guid", []))
            owner_view_count = ep.get("viewCount") or 0
            users = []
            for acc in accounts:
                if providers:
                    is_played = is_item_watched_pwe(providers, "episode", {acc["id"]}, played)
                    if not is_played and acc["id"] == owner_id and owner_view_count > 0:
                        is_played = True
                    last_ts = get_last_played_pwe(providers, "episode", acc["id"], played_ts)
                    if not last_ts and acc["id"] == owner_id and owner_view_count > 0:
                        last_ts = ts_to_iso(ep.get("lastViewedAt"))
                    users.append({"userId": acc["id"], "userName": acc["name"],
                                  "played": is_played, "playCount": 1 if is_played else 0,
                                  "lastPlayedDate": last_ts, "playedPercentage": 100.0 if is_played else 0.0})
                else:
                    is_played = acc["id"] == owner_id and owner_view_count > 0
                    last_ts = ts_to_iso(ep.get("lastViewedAt")) if is_played else None
                    users.append({"userId": acc["id"], "userName": acc["name"],
                                  "played": is_played, "playCount": 1 if is_played else 0,
                                  "lastPlayedDate": last_ts, "playedPercentage": 100.0 if is_played else 0.0})
            result.append({"id": eid, "name": ep.get("title", ""),
                           "indexNumber": ep.get("index", 0),
                           "runTimeTicks": (ep.get("duration") or 0) * 10000,
                           "users": users})
        return jsonify(result)

    db.close()
    # No webhook data: only owner's viewCount per episode is reliable.
    owner_id = plex_owner_id()
    mc = plex_get(f"/library/metadata/{season_id}/children")
    episodes = [ep for ep in mc.get("Metadata", []) if ep.get("type") == "episode"]
    result = []
    for ep in episodes:
        eid = str(ep["ratingKey"])
        view_count = ep.get("viewCount") or 0
        view_offset = ep.get("viewOffset") or 0
        duration_ms = ep.get("duration") or 1
        owner_played = view_count > 0
        owner_pct = 100.0 if owner_played else round(min(view_offset / duration_ms * 100, 99.9), 1)
        users = []
        for acc in accounts:
            if acc["id"] == owner_id:
                users.append({"userId": acc["id"], "userName": acc["name"],
                              "played": owner_played, "playCount": view_count,
                              "lastPlayedDate": ts_to_iso(ep.get("lastViewedAt")),
                              "playedPercentage": owner_pct})
            else:
                users.append({"userId": acc["id"], "userName": acc["name"],
                              "played": False, "playCount": 0,
                              "lastPlayedDate": None, "playedPercentage": 0})
        result.append({"id": eid, "name": ep.get("title", ""),
                       "indexNumber": ep.get("index", 0),
                       "runTimeTicks": (ep.get("duration") or 0) * 10000,
                       "users": users})
    return jsonify(sorted(result, key=lambda x: x["indexNumber"]))

@app.route("/api/watch-status/<item_id>")
@login_required_api
def api_watch_status(item_id):
    item_id = vid(item_id)
    accounts = plex_accounts()

    db = get_db()
    has_pwe = pwe_has_data(db)

    if has_pwe:
        owner_id = plex_owner_id()
        mc = plex_get(f"/library/metadata/{item_id}", {"includeGuids": 1})
        item = (mc.get("Metadata") or [{}])[0]
        providers = parse_plex_guids(item.get("Guid", []))
        raw_type = item.get("type", "movie")
        all_ids = [a["id"] for a in accounts]

        if raw_type == "show":
            # Aggregate episode events
            try:
                leaf_mc = plex_get(f"/library/metadata/{item_id}/allLeaves", {"includeGuids": 1})
                eps = [parse_plex_guids(e.get("Guid", [])) for e in leaf_mc.get("Metadata", [])]
                eps = [p for p in eps if p]
            except Exception:
                eps = []
            ep_played = pwe_get_played(db, all_ids, ["episode"])
            ep_played_ts = pwe_get_played_with_ts(db, all_ids, ["episode"])
            db.close()
            leaf = item.get("leafCount") or 0
            viewed = item.get("viewedLeafCount") or 0
            owner_saw_all = leaf > 0 and viewed >= leaf
            out = []
            for acc in accounts:
                if acc["id"] == owner_id and owner_saw_all:
                    is_played = True
                elif eps:
                    is_played = all(is_item_watched_pwe(p, "episode", {acc["id"]}, ep_played) for p in eps)
                else:
                    is_played = False
                last_ts = None
                if is_played and eps:
                    for p in eps:
                        t = get_last_played_pwe(p, "episode", acc["id"], ep_played_ts)
                        if t and (last_ts is None or t > last_ts): last_ts = t
                out.append({"userId": acc["id"], "userName": acc["name"], "played": is_played,
                            "playCount": 1 if is_played else 0, "lastPlayedDate": last_ts,
                            "playedPercentage": 100.0 if is_played else 0.0})
        else:
            ws_itype = {"movie": "movie", "episode": "episode"}.get(raw_type, "movie")
            played = pwe_get_played(db, all_ids, [ws_itype])
            played_ts = pwe_get_played_with_ts(db, all_ids, [ws_itype])
            db.close()
            owner_saw_it = (item.get("viewCount") or 0) > 0
            out = []
            for acc in accounts:
                if acc["id"] == owner_id and owner_saw_it:
                    is_played = True
                    last_ts = ts_to_iso(item.get("lastViewedAt"))
                elif providers:
                    is_played = is_item_watched_pwe(providers, ws_itype, {acc["id"]}, played)
                    last_ts = get_last_played_pwe(providers, ws_itype, acc["id"], played_ts)
                else:
                    is_played = False
                    last_ts = None
                out.append({"userId": acc["id"], "userName": acc["name"], "played": is_played,
                            "playCount": 1 if is_played else 0, "lastPlayedDate": last_ts,
                            "playedPercentage": 100.0 if is_played else 0.0})
        return jsonify(out)

    db.close()
    # No webhook data: fetch item once with admin token, attribute watch state to owner only.
    owner_id = plex_owner_id()
    out = []
    try:
        mc = plex_get(f"/library/metadata/{item_id}")
        i = (mc.get("Metadata") or [])[0]
        view_count = i.get("viewCount") or 0
        view_offset = i.get("viewOffset") or 0
        duration_ms = i.get("duration") or 1
        owner_played = view_count > 0
        owner_pct = 100.0 if owner_played else round(min(view_offset / duration_ms * 100, 99.9), 1)
        owner_date = ts_to_iso(i.get("lastViewedAt"))
    except Exception:
        owner_played, owner_pct, owner_date, view_count = False, 0, None, 0
    for acc in accounts:
        if acc["id"] == owner_id:
            out.append({"userId": acc["id"], "userName": acc["name"], "played": owner_played,
                        "playCount": view_count, "lastPlayedDate": owner_date,
                        "playedPercentage": owner_pct})
        else:
            out.append({"userId": acc["id"], "userName": acc["name"], "played": False,
                        "playCount": 0, "lastPlayedDate": None, "playedPercentage": 0})
    return jsonify(out)

def _bulk_metadata_by_keys(rating_keys, include_guids=False):
    """Batch /library/metadata/<csv> fetch. Returns dict {ratingKey: item}.

    Replaces per-item /library/metadata/{id} loops on the home page.
    Plex supports comma-joined ratingKeys natively (see api_backfill_history).
    Chunked at 100 to stay under URL length limits.
    """
    keys = [str(k) for k in rating_keys if k]
    if not keys:
        return {}
    out = {}
    params = {"includeGuids": 1} if include_guids else None
    for i in range(0, len(keys), 100):
        chunk = keys[i:i + 100]
        try:
            mc = plex_get(f"/library/metadata/{','.join(chunk)}", params)
        except Exception:
            continue
        for it in (mc.get("Metadata") or []):
            rk = str(it.get("ratingKey") or "")
            if rk:
                out[rk] = it
    return out

@app.route("/api/recent/movies")
@login_required_api
def api_recent_movies():
    cfg = load_config()
    monitored = set(cfg.get("monitored_libraries", []))
    show_all = cfg.get("show_all_libraries", True)
    sections = [s for s in plex_sections()
                if s.get("type") == "movie"
                and (show_all or str(s["key"]) in monitored)]
    section_ids = {str(s["key"]) for s in sections}
    result = {}  # ratingKey -> item dict

    # Admin's native watch history (pre-webhook data)
    for sec in sections:
        try:
            mc = plex_get(f"/library/sections/{sec['key']}/all",
                          {"type": 1, "sort": "lastViewedAt:desc", "X-Plex-Container-Size": 100})
            for i in mc.get("Metadata", []):
                lv = i.get("lastViewedAt")
                if not lv or not (i.get("viewCount") or 0): continue
                rid = str(i["ratingKey"])
                result[rid] = {"id": rid, "name": i.get("title", ""),
                    "year": i.get("year"),
                    "imageUrl": f"/api/image/{rid}?type=Primary&maxWidth=200",
                    "lastPlayedDate": ts_to_iso(lv)}
        except Exception: continue

    # All-user watch history via webhook events
    db = get_db()
    rows = db.execute("""
        SELECT rating_key, provider_type, provider_id, MAX(updated_at) as latest_at
        FROM plex_watch_events
        WHERE event_type='play' AND item_type='movie'
        GROUP BY provider_type, provider_id
        ORDER BY latest_at DESC LIMIT 200
    """).fetchall()

    missing = {(r["provider_type"], r["provider_id"]) for r in rows if not r["rating_key"]}
    resolved = pwe_resolve_rating_keys(db, sections, "movie", missing) if missing else {}
    db.close()

    ts_by_rk = {}
    for row in rows:
        rid = row["rating_key"] or resolved.get((row["provider_type"], row["provider_id"]), "")
        if not rid: continue
        ts = row["latest_at"]
        if (result.get(rid, {}).get("lastPlayedDate") or "") >= ts:
            continue
        ts_by_rk[rid] = ts

    items = _bulk_metadata_by_keys(ts_by_rk.keys())
    for rid, i in items.items():
        if str(i.get("librarySectionID", "")) not in section_ids:
            continue
        ts = ts_by_rk.get(rid)
        if not ts: continue
        result[rid] = {"id": rid, "name": i.get("title", ""),
            "year": i.get("year"),
            "imageUrl": f"/api/image/{rid}?type=Primary&maxWidth=200",
            "lastPlayedDate": ts}

    out = sorted(result.values(), key=lambda x: x.get("lastPlayedDate") or "", reverse=True)
    page = request.args.get("page", type=int)
    if page is not None:
        page = max(1, page)
        offset = (page - 1) * PAGE_SIZE
        return jsonify({"items": out[offset:offset + PAGE_SIZE], "totalCount": len(out),
            "page": page, "pageSize": PAGE_SIZE, "isSearch": False})
    limit = request.args.get("limit", 10, type=int)
    return jsonify(out[:limit])

@app.route("/api/recent/episodes")
@login_required_api
def api_recent_episodes():
    cfg = load_config()
    monitored = set(cfg.get("monitored_libraries", []))
    show_all = cfg.get("show_all_libraries", True)
    sections = [s for s in plex_sections()
                if s.get("type") == "show"
                and (show_all or str(s["key"]) in monitored)]
    section_ids = {str(s["key"]) for s in sections}
    result = {}  # ratingKey -> item dict

    # Admin's native watch history
    for sec in sections:
        try:
            mc = plex_get(f"/library/sections/{sec['key']}/all",
                          {"type": 4, "sort": "lastViewedAt:desc", "X-Plex-Container-Size": 100})
            for i in mc.get("Metadata", []):
                lv = i.get("lastViewedAt")
                if not lv or not (i.get("viewCount") or 0): continue
                rid = str(i["ratingKey"])
                series_id = str(i.get("grandparentRatingKey", ""))
                result[rid] = {"id": rid, "name": i.get("title", ""),
                    "seriesName": i.get("grandparentTitle", ""),
                    "seasonName": i.get("parentTitle", ""),
                    "episodeNumber": i.get("index"),
                    "imageUrl": f"/api/image/{series_id or rid}?type=Primary&maxWidth=200",
                    "lastPlayedDate": ts_to_iso(lv), "seriesId": series_id}
        except Exception: continue

    # All-user watch history via webhook events
    db = get_db()
    rows = db.execute("""
        SELECT rating_key, provider_type, provider_id, MAX(updated_at) as latest_at
        FROM plex_watch_events
        WHERE event_type='play' AND item_type='episode'
        GROUP BY provider_type, provider_id
        ORDER BY latest_at DESC LIMIT 200
    """).fetchall()

    missing = {(r["provider_type"], r["provider_id"]) for r in rows if not r["rating_key"]}
    resolved = pwe_resolve_rating_keys(db, sections, "episode", missing) if missing else {}
    db.close()

    ts_by_rk = {}
    for row in rows:
        rid = row["rating_key"] or resolved.get((row["provider_type"], row["provider_id"]), "")
        if not rid: continue
        ts = row["latest_at"]
        if (result.get(rid, {}).get("lastPlayedDate") or "") >= ts:
            continue
        ts_by_rk[rid] = ts

    items = _bulk_metadata_by_keys(ts_by_rk.keys())
    for rid, i in items.items():
        if str(i.get("librarySectionID", "")) not in section_ids:
            continue
        ts = ts_by_rk.get(rid)
        if not ts: continue
        series_id = str(i.get("grandparentRatingKey", ""))
        result[rid] = {"id": rid, "name": i.get("title", ""),
            "seriesName": i.get("grandparentTitle", ""),
            "seasonName": i.get("parentTitle", ""),
            "episodeNumber": i.get("index"),
            "imageUrl": f"/api/image/{series_id or rid}?type=Primary&maxWidth=200",
            "lastPlayedDate": ts, "seriesId": series_id}

    out = sorted(result.values(), key=lambda x: x.get("lastPlayedDate") or "", reverse=True)
    page = request.args.get("page", type=int)
    if page is not None:
        page = max(1, page)
        offset = (page - 1) * PAGE_SIZE
        return jsonify({"items": out[offset:offset + PAGE_SIZE], "totalCount": len(out),
            "page": page, "pageSize": PAGE_SIZE, "isSearch": False})
    limit = request.args.get("limit", 10, type=int)
    return jsonify(out[:limit])

def _import_plex_history(max_pages=None):
    """Pull play events from Plex's session history into plex_watch_events.

    Used by both the manual Import button and the background poll. Plex's
    media.scrobble webhook does not fire for managed (Plex Home) users, so
    polling /status/sessions/history/all is the only way to keep their watch
    state current. INSERT OR IGNORE makes repeated polls safe.

    With max_pages=None, fetches the entire history (manual backfill).
    With max_pages=N, fetches at most N pages of 500 — enough for incremental
    polling since Plex returns viewedAt:desc.
    """
    accounts = plex_accounts()
    if not accounts:
        return {"ok": False, "error": "no accounts", "imported": 0, "total_history": 0}
    valid_ids = {a["id"] for a in accounts}

    batch = 500
    start = 0
    pages = 0
    all_history = []
    while True:
        try:
            mc = plex_get("/status/sessions/history/all", {
                "sort": "viewedAt:desc",
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": batch,
            })
        except Exception:
            break
        items = mc.get("Metadata") or mc.get("Video") or []
        all_history.extend(items)
        total = int(mc.get("totalSize") or mc.get("size") or 0)
        start += len(items)
        pages += 1
        if not items or start >= total:
            break
        if max_pages is not None and pages >= max_pages:
            break

    target_types = {"movie", "episode"}
    filtered = [
        i for i in all_history
        if (i.get("type") or "").lower() in target_types
        and str(i.get("accountID") or "") in valid_ids
        and i.get("ratingKey")
        and i.get("viewedAt")
    ]

    unique_keys = list({str(i["ratingKey"]) for i in filtered})
    guid_map = {}
    for i in range(0, len(unique_keys), 100):
        chunk = unique_keys[i:i + 100]
        try:
            mc = plex_get(f"/library/metadata/{','.join(chunk)}", {"includeGuids": 1})
            for item in (mc.get("Metadata") or []):
                rk = str(item.get("ratingKey", ""))
                providers = parse_plex_guids(item.get("Guid", []))
                if rk and providers:
                    guid_map[rk] = providers
        except Exception:
            continue

    db = get_db()
    imported = 0
    try:
        for item in filtered:
            rk = str(item["ratingKey"])
            providers = guid_map.get(rk)
            if not providers:
                continue
            account_id = str(item["accountID"])
            item_type = (item.get("type") or "").lower()
            iso_ts = datetime.fromtimestamp(item["viewedAt"], tz=timezone.utc).isoformat()
            for ptype, pid in providers.items():
                cur = db.execute(
                    """INSERT OR IGNORE INTO plex_watch_events
                       (plex_account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key)
                       VALUES (?, ?, ?, ?, 'play', ?, ?)""",
                    (account_id, ptype.lower(), str(pid), item_type, iso_ts, rk)
                )
                imported += cur.rowcount
        db.commit()
    finally:
        db.close()

    return {"ok": True, "imported": imported, "total_history": len(filtered)}


@app.route("/api/admin/backfill-history", methods=["POST"])
@limiter.limit("2 per hour")
@login_required_api
def api_backfill_history():
    """Import historical play events from Plex's session history for all accounts."""
    result = _import_plex_history()
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/admin/debug-history")
@login_required_api
def api_debug_history():
    """Return raw rows from /status/sessions/history/all for diagnosis.

    Optional ?key=<ratingKey> (matches item/parent/grandparent ratingKey) and
    ?title=<substring> (case-insensitive on title/grandparentTitle/parentTitle).
    Used to verify whether a given mark-as-watched produced a history entry.
    """
    key = (request.args.get("key") or "").strip()
    title_q = (request.args.get("title") or "").strip().lower()
    try:
        mc = plex_get("/status/sessions/history/all", {
            "sort": "viewedAt:desc",
            "X-Plex-Container-Start": 0,
            "X-Plex-Container-Size": 500,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    items = mc.get("Metadata") or mc.get("Video") or []
    out = []
    for i in items:
        if key and key not in (
            str(i.get("ratingKey") or ""),
            str(i.get("parentRatingKey") or ""),
            str(i.get("grandparentRatingKey") or ""),
        ):
            continue
        if title_q:
            blob = " ".join(str(i.get(k) or "") for k in ("title", "grandparentTitle", "parentTitle")).lower()
            if title_q not in blob:
                continue
        viewed_at = i.get("viewedAt")
        out.append({
            "ratingKey": i.get("ratingKey"),
            "parentRatingKey": i.get("parentRatingKey"),
            "grandparentRatingKey": i.get("grandparentRatingKey"),
            "type": i.get("type"),
            "title": i.get("title"),
            "grandparentTitle": i.get("grandparentTitle"),
            "season": i.get("parentIndex"),
            "episode": i.get("index"),
            "accountID": i.get("accountID"),
            "viewedAt": viewed_at,
            "viewedAtISO": datetime.fromtimestamp(viewed_at, tz=timezone.utc).isoformat() if viewed_at else None,
        })
    return jsonify({
        "ok": True,
        "total_in_page": len(items),
        "matches": len(out),
        "accounts": plex_accounts(),
        "sample_raw": items[0] if items else None,
        "results": out,
    })


# Background poll: re-imports recent Plex history so managed (Plex Home) users —
# whose plays do not generate media.scrobble webhooks — show up without manual
# intervention. Set PLEX_HISTORY_POLL_INTERVAL=0 to disable.
HISTORY_POLL_INTERVAL = int(os.environ.get("PLEX_HISTORY_POLL_INTERVAL", "300"))
HISTORY_POLL_PAGES = int(os.environ.get("PLEX_HISTORY_POLL_PAGES", "1"))

def _history_poll_sweep():
    try:
        if get_plex_token() and get_plex_url():
            result = _import_plex_history(max_pages=HISTORY_POLL_PAGES)
            if result.get("imported"):
                app.logger.info(f"History poll: imported {result['imported']} new play events")
    except Exception as e:
        app.logger.warning(f"History poll error: {e}")
    finally:
        if HISTORY_POLL_INTERVAL > 0:
            t = threading.Timer(HISTORY_POLL_INTERVAL, _history_poll_sweep)
            t.daemon = True
            t.start()


if HISTORY_POLL_INTERVAL > 0:
    # Delay first run so init_db and the auto-delete sweep finish first.
    _t = threading.Timer(HISTORY_POLL_INTERVAL, _history_poll_sweep)
    _t.daemon = True
    _t.start()


# Per-Plex-Home-user viewCount sweep. Plex does not write history rows
# for "Mark as Watched" actions on managed users and the media.scrobble
# webhook also does not fire for them, so neither the webhook handler nor
# _history_poll_sweep can pick those up. The only signal is viewCount on
# /library/* responses, but viewCount is per-token — we have to query with
# each managed user's own token. We mint those tokens on demand via
# plex.tv's /api/home/users/<id>/switch.
#
# DISABLED BY DEFAULT: in practice some PMS deployments (including PMS
# hosted on seedboxes accessed via .plex.direct) reject the minted Home
# tokens with 401, leaving the sweep with nothing to write. And even when
# the sweep works, Plex's PMS doesn't emit any signal for managed-user
# "Mark as Watched" (Tautulli confirms it can't see them either), so the
# sweep is also the only path for that case. Set
# PLEX_MANAGED_USER_SWEEP_INTERVAL to a positive value (e.g. 300) to
# enable it for setups where the per-user tokens are accepted. The
# /api/admin/debug-managed-sweep endpoint can run a single cycle
# on demand regardless of this setting.
MANAGED_USER_SWEEP_INTERVAL = int(os.environ.get("PLEX_MANAGED_USER_SWEEP_INTERVAL", "0"))

_managed_user_token_cache = {}  # plex.tv user id -> (token, mint_monotonic_time)
_MANAGED_TOKEN_TTL = 3600  # refresh hourly; switch tokens stay valid much longer in practice

def _get_managed_user_token(home_user_id):
    now = time.monotonic()
    cached = _managed_user_token_cache.get(home_user_id)
    if cached and now - cached[1] < _MANAGED_TOKEN_TTL:
        return cached[0]
    try:
        token = plex_tv_switch_token(home_user_id)
    except Exception as e:
        app.logger.warning(f"Switch-user token mint failed for home id {home_user_id}: {e}")
        return None
    if token:
        _managed_user_token_cache[home_user_id] = (token, now)
    return token

def _run_managed_user_sweep():
    """Run one cycle of the managed-user sweep, returning a diagnostic report.

    Separated from the timer loop so /api/admin/debug-managed-sweep can call
    it synchronously and surface what each step is doing.
    """
    report = {"users": [], "imported": 0, "skipped_reason": None, "refresh_status": None}

    if not get_plex_token() or not get_plex_url():
        report["skipped_reason"] = "no plex token or url"
        return report

    # Make the PMS sync its authorized-tokens list from plex.tv before we
    # start querying with per-user switch tokens, otherwise the PMS rejects
    # newly-minted tokens with 401.
    report["refresh_status"] = plex_refresh_access_tokens()

    try:
        home_users = plex_tv_home_users()
    except Exception as e:
        report["skipped_reason"] = f"plex.tv home users fetch failed: {e}"
        return report

    if not home_users:
        report["skipped_reason"] = "no home users returned"
        return report

    try:
        local_accounts = plex_all_accounts()
    except Exception as e:
        report["skipped_reason"] = f"local accounts fetch failed: {e}"
        return report
    # Case-insensitive name match; plex.tv title and local /accounts name don't
    # always agree on case (e.g. "PRINCESS" vs "Princess").
    local_by_name = {a["name"].strip().lower(): a["id"] for a in local_accounts}

    try:
        sections = plex_sections()
    except Exception as e:
        report["skipped_reason"] = f"sections fetch failed: {e}"
        return report
    target_sections = [s for s in sections if s.get("type") in ("movie", "show")]
    if not target_sections:
        report["skipped_reason"] = "no movie/show sections"
        return report

    db = get_db()
    first = True
    try:
        for hu in home_users:
            entry = {
                "home_id": hu.get("id"),
                "title": hu.get("title"),
                "admin": hu.get("admin"),
                "protected": hu.get("protected"),
                "local_account_id": None,
                "token_ok": False,
                "sections": [],
                "imported": 0,
                "skipped_reason": None,
            }
            if hu.get("admin"):
                entry["skipped_reason"] = "admin (owner already covered by webhook)"
                report["users"].append(entry)
                continue
            title = (hu.get("title") or "").strip()
            if not title:
                entry["skipped_reason"] = "no title"
                report["users"].append(entry)
                continue
            local_id = local_by_name.get(title.lower())
            entry["local_account_id"] = local_id
            if not local_id:
                entry["skipped_reason"] = "no matching local /accounts entry"
                report["users"].append(entry)
                continue
            if hu.get("protected"):
                entry["skipped_reason"] = "PIN-protected"
                report["users"].append(entry)
                continue
            # plex.tv 429s aggressively if we mint several switch tokens in
            # rapid succession. Space them out a little when we know the cache
            # is cold (no token yet for this user).
            if not first and hu.get("id") not in _managed_user_token_cache:
                time.sleep(1.5)
            first = False

            user_token = _get_managed_user_token(hu.get("id"))
            if not user_token:
                diag = plex_tv_switch_token_diag(hu.get("id"))
                entry["switch_diag"] = diag
                entry["skipped_reason"] = f"switch token mint failed (status={diag.get('status')}, endpoint={diag.get('endpoint')})"
                report["users"].append(entry)
                continue
            entry["token_ok"] = True

            for s in target_sections:
                section_key = s.get("key")
                section_type = s.get("type")
                item_type_code = "1" if section_type == "movie" else "4"
                item_type = "movie" if section_type == "movie" else "episode"
                sect_entry = {
                    "section_key": section_key,
                    "section_title": s.get("title"),
                    "watched_returned": 0,
                    "imported": 0,
                    "error": None,
                }
                try:
                    mc = plex_get_with_token(
                        f"/library/sections/{section_key}/all",
                        user_token,
                        {"type": item_type_code, "unwatched": 0, "includeGuids": 1,
                         "X-Plex-Container-Size": 10000},
                    )
                except Exception as e:
                    sect_entry["error"] = str(e)
                    entry["sections"].append(sect_entry)
                    continue

                items = mc.get("Metadata") or []
                sect_entry["watched_returned"] = len(items)
                for item in items:
                    rk = str(item.get("ratingKey") or "")
                    if not rk:
                        continue
                    if (item.get("viewCount") or 0) <= 0:
                        continue
                    providers = parse_plex_guids(item.get("Guid") or [])
                    if not providers:
                        continue
                    last_viewed = item.get("lastViewedAt")
                    if not last_viewed:
                        continue
                    iso_ts = datetime.fromtimestamp(last_viewed, tz=timezone.utc).isoformat()
                    for ptype, pid in providers.items():
                        cur = db.execute(
                            """INSERT OR IGNORE INTO plex_watch_events
                               (plex_account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key)
                               VALUES (?, ?, ?, ?, 'play', ?, ?)""",
                            (local_id, ptype.lower(), str(pid), item_type, iso_ts, rk)
                        )
                        sect_entry["imported"] += cur.rowcount
                        entry["imported"] += cur.rowcount
                        report["imported"] += cur.rowcount
                entry["sections"].append(sect_entry)
            report["users"].append(entry)
        db.commit()
    finally:
        db.close()

    return report

def _managed_user_sweep():
    try:
        report = _run_managed_user_sweep()
        if report.get("skipped_reason"):
            app.logger.info(f"Managed-user sweep skipped: {report['skipped_reason']}")
        else:
            app.logger.info(f"Managed-user sweep: imported {report['imported']} events across {len(report['users'])} users")
    except Exception as e:
        app.logger.warning(f"Managed-user sweep error: {e}")
    finally:
        if MANAGED_USER_SWEEP_INTERVAL > 0:
            t = threading.Timer(MANAGED_USER_SWEEP_INTERVAL, _managed_user_sweep)
            t.daemon = True
            t.start()


@app.route("/api/admin/debug-managed-sweep", methods=["GET", "POST"])
@login_required_api
def api_debug_managed_sweep():
    """Run one managed-user sweep synchronously and return the per-user report."""
    return jsonify(_run_managed_user_sweep())


if MANAGED_USER_SWEEP_INTERVAL > 0:
    # Delay first run so init and the other sweeps settle first.
    _mu_t = threading.Timer(60, _managed_user_sweep)
    _mu_t.daemon = True
    _mu_t.start()

@app.route("/api/watch-summary/items")
@login_required_api
def api_watch_summary_items():
    raw_movie_ids = [x for x in (request.args.get("movieIds", "") or "").split(",") if x]
    raw_ep_ids   = [x for x in (request.args.get("episodeIds", "") or "").split(",") if x]
    movie_ids = [i for i in raw_movie_ids if _ID_RE.match(i)]
    ep_ids    = [i for i in raw_ep_ids    if _ID_RE.match(i)]
    if not movie_ids and not ep_ids:
        return jsonify({})

    accounts = plex_accounts()
    if not accounts:
        return jsonify({})
    all_account_ids = {a["id"] for a in accounts}
    owner_id = plex_owner_id()

    db = get_db()
    item_assignments, provider_assignments = _load_assignment_maps(db)
    has_pwe = pwe_has_data(db)

    def resolve_target(providers, item_id):
        return _resolve_target(providers, item_id, item_assignments, provider_assignments, all_account_ids)

    result = {}
    played = pwe_get_played(db, list(all_account_ids), ["movie", "episode"]) if has_pwe else {}
    db.close()

    # One batched call for every movie/episode id, plus one for the
    # distinct grandparent (show) ids the episodes belong to.
    items_by_id = _bulk_metadata_by_keys({*movie_ids, *ep_ids}, include_guids=True)

    grandparent_ids = {str(items_by_id.get(eid, {}).get("grandparentRatingKey") or "")
                       for eid in ep_ids}
    grandparent_ids.discard("")
    show_meta = _bulk_metadata_by_keys(grandparent_ids, include_guids=True) if grandparent_ids else {}
    show_providers_by_id = {gid: parse_plex_guids(it.get("Guid", []))
                            for gid, it in show_meta.items()}

    for iid in movie_ids:
        item = items_by_id.get(iid)
        if not item:
            result[iid] = {"watched": 0, "total": 0, "watcher_ids": []}
            continue
        providers = parse_plex_guids(item.get("Guid", []))
        target_ids = resolve_target(providers, iid)
        owner_view_count = item.get("viewCount") or 0
        if has_pwe:
            watched_count = 0; watcher_ids = []
            for aid in target_ids:
                saw_it = (aid == owner_id and owner_view_count > 0) or (
                    bool(providers) and is_item_watched_pwe(providers, "movie", {aid}, played))
                if saw_it:
                    watched_count += 1; watcher_ids.append(aid)
            result[iid] = {"watched": watched_count, "total": len(target_ids), "watcher_ids": watcher_ids}
        else:
            owner_saw_it = owner_view_count > 0 and owner_id in target_ids
            wids = [owner_id] if owner_saw_it else []
            result[iid] = {"watched": len(wids), "total": len(target_ids), "watcher_ids": wids}

    for iid in ep_ids:
        item = items_by_id.get(iid)
        if not item:
            result[iid] = {"watched": 0, "total": 0, "watcher_ids": []}
            continue
        providers = parse_plex_guids(item.get("Guid", []))
        grandparent_id = str(item.get("grandparentRatingKey", ""))
        show_providers = show_providers_by_id.get(grandparent_id, {}) if grandparent_id else {}
        target_ids = resolve_target(show_providers, grandparent_id)
        owner_view_count = item.get("viewCount") or 0
        if has_pwe:
            watched_count = 0; watcher_ids = []
            for aid in target_ids:
                saw_it = is_item_watched_pwe(providers, "episode", {aid}, played) if providers else False
                if not saw_it and aid == owner_id and owner_view_count > 0:
                    saw_it = True
                if saw_it:
                    watched_count += 1; watcher_ids.append(aid)
            result[iid] = {"watched": watched_count, "total": len(target_ids), "watcher_ids": watcher_ids}
        else:
            owner_saw_it = owner_view_count > 0 and owner_id in target_ids
            wids = [owner_id] if owner_saw_it else []
            result[iid] = {"watched": len(wids), "total": len(target_ids), "watcher_ids": wids}

    return jsonify(result)

@app.route("/api/activity")
@login_required_api
def api_activity():
    try:
        mc = plex_get("/status/sessions")
    except Exception:
        return jsonify([])
    # Plex uses "Metadata" in newer versions, "Video"/"Track" in older ones
    sessions = mc.get("Metadata") or mc.get("Video") or mc.get("Track") or []
    out = []
    for i in sessions:
        rid = str(i.get("ratingKey", ""))
        view_offset = i.get("viewOffset") or 0
        duration = i.get("duration") or 1
        progress = round(min(view_offset / duration * 100, 100), 1)
        media = i.get("Media") or []
        part_decision = ""
        if media and media[0].get("Part"):
            part_decision = (media[0]["Part"][0].get("decision") or "").lower()
        stream_type = "Direct Play" if part_decision == "directplay" else ("Transcode" if part_decision == "transcode" else "")
        bandwidth_kbps = (i.get("Session") or {}).get("bandwidth") or 0
        bandwidth_mbps = round(bandwidth_kbps / 1000, 1) if bandwidth_kbps else None
        out.append({
            "id": (i.get("Session") or {}).get("id") or rid,
            "type": i.get("type", ""),
            "title": i.get("title", ""),
            "seriesName": i.get("grandparentTitle") or None,
            "seasonName": i.get("parentTitle") or None,
            "episodeNumber": i.get("index"),
            "imageUrl": f"/api/image/{rid}?type=Primary&maxWidth=300" if rid else None,
            "progress": progress,
            "viewOffset": view_offset,
            "duration": duration,
            "user": (i.get("User") or {}).get("title", "Unknown"),
            "player": (i.get("Player") or {}).get("title", ""),
            "streamType": stream_type,
            "bandwidth": bandwidth_mbps,
            "ratingKey": rid,
            "grandparentRatingKey": str(i.get("grandparentRatingKey") or ""),
            "parentRatingKey": str(i.get("parentRatingKey") or ""),
            "librarySectionID": str(i.get("librarySectionID") or ""),
        })
    return jsonify(out)

@app.route("/api/events/last-update")
@login_required_api
def api_events_last_update():
    db = get_db()
    row = db.execute("SELECT MAX(updated_at) as last_at FROM plex_watch_events").fetchone()
    db.close()
    return jsonify({"last_at": row["last_at"] if row else None})

@app.route("/api/accounts")
@login_required_api
def api_accounts_all():
    cfg = load_config()
    hidden = set(cfg.get("hidden_accounts", []))
    all_accs = plex_all_accounts()
    return jsonify([{"id": a["id"], "name": a["name"] or f"(unnamed #{a['id']})",
                     "hidden": a["id"] in hidden} for a in all_accs])

@app.route("/api/accounts/<account_id>/hide", methods=["POST"])
@login_required_api
def api_hide_account(account_id):
    account_id = vid(account_id)
    cfg = load_config()
    hidden = set(cfg.get("hidden_accounts", []))
    hidden.add(account_id)
    cfg["hidden_accounts"] = list(hidden)
    save_config(cfg)
    return jsonify({"success": True})

@app.route("/api/accounts/<account_id>/hide", methods=["DELETE"])
@login_required_api
def api_unhide_account(account_id):
    account_id = vid(account_id)
    cfg = load_config()
    hidden = set(cfg.get("hidden_accounts", []))
    hidden.discard(account_id)
    cfg["hidden_accounts"] = list(hidden)
    save_config(cfg)
    return jsonify({"success": True})

@app.route("/api/assignments/<item_id>")
@login_required_api
def api_get_assignments(item_id):
    item_id = vid(item_id)
    assigned = get_assigned_ids(item_id)
    if assigned is None: return jsonify({"assigned": [], "mode": "all"})
    return jsonify({"assigned": list(assigned), "mode": "custom"})

@app.route("/api/recent/added")
@login_required_api
def api_recent_added():
    try:
        limit = min(500, max(1, int(request.args.get("limit", "60"))))
    except (ValueError, TypeError):
        limit = 60
    return jsonify(recently_added.get_recent(limit=limit))

@app.route("/api/assignments/<item_id>", methods=["POST"])
@login_required_api
def api_set_assignments(item_id):
    item_id = vid(item_id)
    user_ids = request.json.get("userIds", [])
    user_ids = [str(u) for u in user_ids if _ID_RE.match(str(u))]
    if not set_assignment_by_provider(item_id, user_ids):
        set_assignment_by_item_id(item_id, user_ids)
    recently_added.mark_handled(item_id)
    return jsonify({"success": True})

@app.route("/api/assignments/<item_id>", methods=["DELETE"])
@login_required_api
def api_delete_assignments(item_id):
    item_id = vid(item_id)
    delete_assignment_by_provider(item_id)
    delete_assignment_by_item_id(item_id)
    recently_added.mark_handled(item_id)
    return jsonify({"success": True})

@app.route("/api/assignments/bulk", methods=["POST"])
@login_required_api
def api_assignments_bulk():
    data = request.json
    item_ids = data.get("item_ids", [])
    user_ids = data.get("user_ids", [])
    if not item_ids or not isinstance(item_ids, list):
        return jsonify({"success": False, "error": "item_ids required"}), 400
    if not user_ids or not isinstance(user_ids, list):
        return jsonify({"success": False, "error": "user_ids required"}), 400
    item_ids = [str(i) for i in item_ids if _ID_RE.match(str(i))]
    user_ids = [str(u) for u in user_ids if _ID_RE.match(str(u))]
    success_count, failed_ids = 0, []
    for item_id in item_ids:
        try:
            if not set_assignment_by_provider(item_id, user_ids):
                set_assignment_by_item_id(item_id, user_ids)
            recently_added.mark_handled(item_id)
            success_count += 1
            app.logger.info(f"Bulk assigned users to {item_id} from {request.remote_addr}")
        except Exception:
            app.logger.exception(f"Bulk assignment failed for item {item_id} from {request.remote_addr}")
            failed_ids.append({"id": item_id, "error": "Failed to assign users to this item"})
    return jsonify({"success": True, "assigned_count": success_count, "failed": failed_ids})

@app.route("/api/auto-delete/series/<series_id>", methods=["GET"])
@login_required_api
def api_auto_delete_series_get(series_id):
    series_id = vid(series_id)
    cfg = load_config()
    db = get_db()
    row = db.execute(
        "SELECT enabled FROM auto_delete_overrides WHERE scope='series' AND scope_id=?",
        (series_id,)).fetchone()
    db.close()
    override = None if row is None else bool(row["enabled"])
    library_id = None
    try:
        mc = plex_get(f"/library/metadata/{series_id}")
        library_id = str((mc.get("Metadata") or [{}])[0].get("librarySectionID") or "")
    except Exception:
        pass
    library_enabled = bool(library_id and str(library_id) in [str(x) for x in (cfg.get("auto_delete_libraries") or [])])
    effective = override if override is not None else library_enabled
    return jsonify({"override": override, "effective": effective, "library_enabled": library_enabled,
                    "global_enabled": bool(cfg.get("auto_delete_enabled")),
                    "grace_days": int(cfg.get("auto_delete_grace_days") or 0)})

@app.route("/api/auto-delete/series/<series_id>", methods=["POST"])
@login_required_api
def api_auto_delete_series_set(series_id):
    series_id = vid(series_id)
    data = request.json or {}
    enabled = data.get("enabled")  # True, False, or None (clear override)
    global_enabled_was_off = False
    db = get_db()
    if enabled is None:
        db.execute("DELETE FROM auto_delete_overrides WHERE scope='series' AND scope_id=?", (series_id,))
    elif enabled:
        now_ts = datetime.now(timezone.utc).isoformat()
        db.execute("""INSERT INTO auto_delete_overrides (scope, scope_id, enabled, enabled_at)
                      VALUES ('series', ?, 1, ?)
                      ON CONFLICT(scope, scope_id) DO UPDATE SET enabled=1, enabled_at=excluded.enabled_at""",
                   (series_id, now_ts))
        cfg = load_config()
        if not cfg.get("auto_delete_enabled"):
            cfg["auto_delete_enabled"] = True
            save_config(cfg)
            global_enabled_was_off = True
    else:
        db.execute("""INSERT INTO auto_delete_overrides (scope, scope_id, enabled)
                      VALUES ('series', ?, 0)
                      ON CONFLICT(scope, scope_id) DO UPDATE SET enabled=0""",
                   (series_id,))
    db.commit(); db.close()
    return jsonify({"success": True, "global_enabled_was_off": global_enabled_was_off})

@app.route("/api/auto-delete/movie/<movie_id>", methods=["GET"])
@login_required_api
def api_auto_delete_movie_get(movie_id):
    movie_id = vid(movie_id)
    cfg = load_config()
    db = get_db()
    row = db.execute(
        "SELECT enabled FROM auto_delete_overrides WHERE scope='movie' AND scope_id=?",
        (movie_id,)).fetchone()
    db.close()
    override = None if row is None else bool(row["enabled"])
    library_id = None
    try:
        mc = plex_get(f"/library/metadata/{movie_id}")
        library_id = str((mc.get("Metadata") or [{}])[0].get("librarySectionID") or "")
    except Exception:
        pass
    library_enabled = bool(library_id and str(library_id) in [str(x) for x in (cfg.get("auto_delete_libraries") or [])])
    effective = override if override is not None else library_enabled
    return jsonify({"override": override, "effective": effective, "library_enabled": library_enabled,
                    "global_enabled": bool(cfg.get("auto_delete_enabled")),
                    "grace_days": int(cfg.get("auto_delete_grace_days") or 0)})

@app.route("/api/auto-delete/movie/<movie_id>", methods=["POST"])
@login_required_api
def api_auto_delete_movie_set(movie_id):
    movie_id = vid(movie_id)
    data = request.json or {}
    enabled = data.get("enabled")
    global_enabled_was_off = False
    db = get_db()
    if enabled is None:
        db.execute("DELETE FROM auto_delete_overrides WHERE scope='movie' AND scope_id=?", (movie_id,))
    elif enabled:
        now_ts = datetime.now(timezone.utc).isoformat()
        db.execute("""INSERT INTO auto_delete_overrides (scope, scope_id, enabled, enabled_at)
                      VALUES ('movie', ?, 1, ?)
                      ON CONFLICT(scope, scope_id) DO UPDATE SET enabled=1, enabled_at=excluded.enabled_at""",
                   (movie_id, now_ts))
        cfg = load_config()
        if not cfg.get("auto_delete_enabled"):
            cfg["auto_delete_enabled"] = True
            save_config(cfg)
            global_enabled_was_off = True
    else:
        db.execute("""INSERT INTO auto_delete_overrides (scope, scope_id, enabled)
                      VALUES ('movie', ?, 0)
                      ON CONFLICT(scope, scope_id) DO UPDATE SET enabled=0""",
                   (movie_id,))
    db.commit(); db.close()
    return jsonify({"success": True, "global_enabled_was_off": global_enabled_was_off})

@app.route("/api/auto-delete/sweep", methods=["POST"])
@login_required_api
def api_auto_delete_sweep():
    """Manually trigger an auto-delete sweep. Returns immediately; sweep runs synchronously."""
    cfg = load_config()
    if not cfg.get("auto_delete_enabled"):
        return jsonify({"success": False, "error": "auto_delete_enabled is False"}), 400
    try:
        _run_sweep(cfg)
        return jsonify({"success": True, "message": "Sweep complete — check audit log for deletions."})
    except Exception:
        app.logger.exception("Manual sweep failed")
        return jsonify({"success": False, "error": "Internal server error"}), 500


@app.route("/api/auto-delete/diagnose/<rating_key>", methods=["GET"])
@login_required_api
def api_auto_delete_diagnose(rating_key):
    """Return a JSON report explaining why auto-delete would or would not fire for this item."""
    rating_key = vid(rating_key)
    cfg = load_config()
    grace_days = int(cfg.get("auto_delete_grace_days") or 0)
    min_delay_minutes = int(cfg.get("auto_delete_min_delay_minutes") or 30)
    global_enabled = bool(cfg.get("auto_delete_enabled"))
    out = {"rating_key": rating_key, "global_enabled": global_enabled, "grace_days": grace_days,
           "min_delay_minutes": min_delay_minutes}

    if not global_enabled:
        out.update({"verdict": "GLOBAL_OFF",
                    "verdict_detail": "auto_delete_enabled is False in Settings. Turn it on to proceed."})
        return jsonify(out)

    # Resolve item metadata
    library_id = None
    series_id = None
    item_type = None
    item_title = rating_key
    providers = {}
    try:
        mc = plex_get(f"/library/metadata/{rating_key}", {"includeGuids": 1})
        m = (mc.get("Metadata") or [{}])[0]
        library_id = str(m.get("librarySectionID") or "")
        item_type = "episode" if m.get("type") == "episode" else ("movie" if m.get("type") == "movie" else m.get("type", "unknown"))
        item_title = m.get("title", rating_key)
        providers = parse_plex_guids(m.get("Guid") or [])
        if item_type == "episode":
            series_id = str(m.get("grandparentRatingKey") or "")
    except Exception as e:
        out.update({"verdict": "PLEX_ERROR",
                    "verdict_detail": f"Could not fetch metadata from Plex: {e}"})
        return jsonify(out)

    out.update({"item_type": item_type, "item_title": item_title,
                "library_id": library_id, "series_id": series_id,
                "providers": providers})

    db = get_db()
    try:
        # Check override/library active state
        override_row = None
        if series_id:
            override_row = db.execute(
                "SELECT enabled FROM auto_delete_overrides WHERE scope='series' AND scope_id=?",
                (series_id,)).fetchone()
        elif item_type == "movie":
            override_row = db.execute(
                "SELECT enabled FROM auto_delete_overrides WHERE scope='movie' AND scope_id=?",
                (rating_key,)).fetchone()
        override = None if override_row is None else bool(override_row["enabled"])
        library_enabled = str(library_id) in [str(x) for x in (cfg.get("auto_delete_libraries") or [])]
        effective = override if override is not None else library_enabled
        out.update({"override": override, "library_enabled": library_enabled, "auto_delete_active": effective})

        if not effective:
            toggle_hint = "per-movie toggle" if item_type == "movie" else "per-show toggle"
            out.update({"verdict": "NOT_ACTIVE",
                        "verdict_detail": f"Auto-delete is not active for this item. Enable it via the {toggle_hint} or by opting the library in via Settings."})
            return jsonify(out)

        # Resolve targets
        all_account_ids = {a["id"] for a in plex_accounts()}
        owner_id = plex_owner_id()
        item_assignments, provider_assignments = _load_assignment_maps(db)
        lookup_id = series_id if (item_type == "episode" and series_id) else rating_key
        show_providers = providers if item_type == "movie" else {}
        if item_type == "episode" and series_id:
            try:
                s_mc = plex_get(f"/library/metadata/{series_id}", {"includeGuids": 1})
                show_providers = parse_plex_guids((s_mc.get("Metadata") or [{}])[0].get("Guid") or [])
            except Exception:
                pass
        target_ids = _resolve_target(show_providers, lookup_id, item_assignments, provider_assignments, all_account_ids)
        out["target_users"] = sorted(target_ids)

        if not target_ids:
            out.update({"verdict": "NO_TARGETS",
                        "verdict_detail": "No users assigned to this item. Assign users in Settings."})
            return jsonify(out)

        # Check per-user watch events
        played = pwe_get_played(db, list(target_ids), [item_type])
        played_rk = pwe_get_played_by_ratingkey(db, list(target_ids), [item_type])
        rk_watchers = played_rk.get(rating_key, set())
        watch_events = []
        all_watched = True
        latest_ts = None
        for aid in target_ids:
            found_by = None
            if bool(providers) and is_item_watched_pwe(providers, item_type, {aid}, played):
                found_by = "provider"
            elif aid in rk_watchers:
                found_by = "rating_key"
            elif owner_id and aid == owner_id and (m.get("viewCount") or 0) > 0:
                found_by = "viewcount"
            else:
                all_watched = False
            # Get timestamp for this user
            user_ts = None
            if providers:
                for ptype, pid in providers.items():
                    row = db.execute(
                        "SELECT updated_at FROM plex_watch_events WHERE plex_account_id=? AND provider_type=? AND provider_id=? AND item_type=? AND event_type='play'",
                        (aid, ptype.lower(), str(pid), item_type)).fetchone()
                    if row and (user_ts is None or row["updated_at"] > user_ts):
                        user_ts = row["updated_at"]
            if user_ts is None:
                row = db.execute(
                    "SELECT updated_at FROM plex_watch_events WHERE plex_account_id=? AND rating_key=? AND event_type='play'",
                    (aid, rating_key)).fetchone()
                if row:
                    user_ts = row["updated_at"]
            if user_ts is None and found_by == "viewcount":
                lva = m.get("lastViewedAt")
                if lva:
                    user_ts = ts_to_iso(lva)
            if user_ts and (latest_ts is None or user_ts > latest_ts):
                latest_ts = user_ts
            watch_events.append({"account_id": aid, "found_by": found_by, "updated_at": user_ts})
        out.update({"watch_events": watch_events, "all_watched": all_watched, "latest_watch_ts": latest_ts})

        if not all_watched:
            out.update({"verdict": "NOT_ALL_WATCHED",
                        "verdict_detail": "At least one assigned user has not watched this item yet."})
            return jsonify(out)

        if latest_ts is None:
            out.update({"verdict": "NO_WATCH_EVENTS",
                        "verdict_detail": "No watch events found in the database for this item."})
            return jsonify(out)

        # Timestamp gate (enabled_since)
        diag_movie_id = rating_key if item_type == "movie" else None
        enabled_since = _get_enabled_since(db, cfg, library_id, series_id, diag_movie_id)
        out["enabled_since"] = enabled_since.isoformat() if enabled_since else None
        try:
            ts_dt = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
        except Exception:
            out.update({"verdict": "TIMESTAMP_PARSE_ERROR",
                        "verdict_detail": f"Could not parse latest watch timestamp: {latest_ts}"})
            return jsonify(out)
        timestamp_gate_passed = enabled_since is None or ts_dt > enabled_since
        out["timestamp_gate_passed"] = timestamp_gate_passed

        if not timestamp_gate_passed:
            out.update({"verdict": "PRE_OPT_IN",
                        "verdict_detail": f"All users have watched, but the last watch event ({latest_ts}) predates when auto-delete was enabled ({enabled_since.isoformat()}). Only watches after opt-in count."})
            return jsonify(out)

        # Grace period
        now = datetime.now(timezone.utc)
        elapsed = now - ts_dt
        elapsed_hours = elapsed.total_seconds() / 3600
        elapsed_minutes = elapsed.total_seconds() / 60
        required_hours = grace_days * 24
        grace_passed = grace_days == 0 or elapsed >= timedelta(days=grace_days)
        min_delay_passed = min_delay_minutes == 0 or elapsed >= timedelta(minutes=min_delay_minutes)
        out.update({"grace_elapsed_hours": round(elapsed_hours, 2),
                    "grace_required_hours": required_hours, "grace_passed": grace_passed,
                    "min_delay_elapsed_minutes": round(elapsed_minutes, 1),
                    "min_delay_required_minutes": min_delay_minutes, "min_delay_passed": min_delay_passed})

        if not grace_passed:
            remaining_h = round(required_hours - elapsed_hours, 1)
            out.update({"verdict": "WAITING_FOR_GRACE",
                        "verdict_detail": f"All users have watched. Waiting for {grace_days}-day grace period (~{remaining_h}h remaining). The sweep will delete this once grace has elapsed."})
            return jsonify(out)

        if not min_delay_passed:
            remaining_m = round(min_delay_minutes - elapsed_minutes, 1)
            out.update({"verdict": "WAITING_FOR_MIN_DELAY",
                        "verdict_detail": f"All users have watched. Waiting for minimum post-scrobble delay ({min_delay_minutes} min, ~{remaining_m} min remaining). The sweep will delete this shortly."})
            return jsonify(out)

        out.update({"verdict": "WOULD_DELETE",
                    "verdict_detail": "All conditions pass. This item would be deleted on the next qualifying event or sweep tick."})
        return jsonify(out)
    finally:
        db.close()


@app.route("/api/delete/<item_id>", methods=["DELETE"])
@limiter.limit("30 per minute")
@login_required_api
def api_delete_item(item_id):
    item_id = vid(item_id)
    try:
        plex_delete(f"/library/metadata/{item_id}")
        app.logger.info(f"Deleted item {item_id} from {request.remote_addr}")
        return jsonify({"success": True})
    except Exception:
        app.logger.exception(f"Failed to delete item {item_id} from {request.remote_addr}")
        return jsonify({"success": False, "error": "Failed to delete item"}), 500

@app.route("/api/delete-batch", methods=["DELETE"])
@limiter.limit("10 per minute")
@login_required_api
def api_delete_batch():
    ids = request.json.get("itemIds", [])
    ids = [str(i) for i in ids if _ID_RE.match(str(i))]
    deleted, failed = [], []
    for iid in ids:
        try:
            plex_delete(f"/library/metadata/{iid}")
            deleted.append(iid)
            app.logger.info(f"Deleted batch item {iid} from {request.remote_addr}")
        except Exception:
            app.logger.exception(f"Failed to delete batch item {iid} from {request.remote_addr}")
            failed.append({"id": iid, "error": "Failed to delete item"})
    return jsonify({"success": True, "deleted": deleted, "failed": failed})

@app.route("/api/check-season-empty/<series_id>/<season_id>")
@login_required_api
def api_check_season_empty(series_id, season_id):
    series_id = vid(series_id); season_id = vid(season_id)
    try:
        mc = plex_get(f"/library/metadata/{season_id}/children")
        episodes = [i for i in mc.get("Metadata", []) if i.get("type") == "episode"]
        return jsonify({"empty": len(episodes) == 0})
    except Exception: return jsonify({"empty": False})

@app.route("/api/image/<item_id>")
@limiter.limit("600 per minute")
@login_required_api
def proxy_image(item_id):
    item_id = vid(item_id)
    try:
        w_int = min(max(50, int(float(request.args.get("maxWidth", "300")))), 1000)
    except (ValueError, TypeError):
        w_int = 300
    w = str(w_int)
    h = str(int(w_int * 1.5))
    try:
        mc = plex_get(f"/library/metadata/{item_id}")
        m = (mc.get("Metadata") or [{}])[0]
        thumb = m.get("thumb") or m.get("parentThumb") or m.get("grandparentThumb")
        if not thumb: return Response(status=404)
        r = plex_get_raw("/photo/:/transcode",
                          {"width": w, "height": h, "minSize": 1, "upscale": 1, "url": thumb})
        return Response(r.iter_content(8192), content_type=r.headers.get("Content-Type", "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception: return Response(status=404)

if __name__ == "__main__":
    if os.path.exists(CONFIG_PATH):
        os.chmod(CONFIG_PATH, 0o600)
    print(f"Plex Watch History Dashboard\n  Port: {PORT}\n")
    if is_setup_needed():
        print(f"  *** First-time setup required ***")
        print(f"  Open http://<your-host>:{PORT} in your browser to complete setup.\n")
    else:
        effective_url = get_plex_url()
        print(f"  Plex: {effective_url}")
        print("  NOTE: Enable 'Allow media deletion' in Plex Settings → Troubleshooting for deletes to remove files from disk.")
        print(f"  Plex webhook URL: http://<your-host>:{PORT}/api/webhook/plex")
        ws = get_webhook_secret()
        if ws:
            print("  Plex webhook secret: configured (append ?secret=... to the URL)")
        else:
            print("  Plex webhook secret: not set (set PLEX_WEBHOOK_SECRET env var to protect the endpoint)")
        print("  Configure in Plex: Settings → Webhooks → Add Webhook (requires Plex Pass)")
    print()
    app.run(host="0.0.0.0", port=PORT, debug=False)
