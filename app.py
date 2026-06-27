#!/usr/bin/env python3
"""Jellyfin Watch History Dashboard with Authentication & Security"""

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
    update_admin_credentials,
)
from db import get_db, init_db
from jellyfin_api import (
    get_jellyfin_url, get_jellyfin_api_key, get_webhook_secret, _jf_headers,
    jellyfin_get, jellyfin_get_item, jellyfin_delete, jellyfin_users,
    jellyfin_all_users, jellyfin_admin_id, jellyfin_libraries, jellyfin_items,
    ts_to_iso, parse_jellyfin_providers, get_item_providers,
    set_request_hook as _jf_set_request_hook,
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

PORT = int(os.environ.get("PORT", "8080"))

# Run DB init at import time so it works under gunicorn/wsgi as well as direct run
init_db()

# Kick off the auto-delete sweep timer at import time. Runs once per process —
# gunicorn is configured with a single worker to keep this from racing on SQLite.
_auto_delete_sweep()

# Discovery sweep for the Recently Added list. Records newly-added titles so the
# list is populated without manual action. Set RECENTLY_ADDED_SWEEP_INTERVAL=0
# to disable. First run is delayed so init_db and Jellyfin config settle first.
RECENTLY_ADDED_SWEEP_INTERVAL = int(os.environ.get("RECENTLY_ADDED_SWEEP_INTERVAL", "1800"))

def _recently_added_sweep():
    try:
        if get_jellyfin_api_key() and get_jellyfin_url():
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
# Format: [PERF] /api/path 200 total=Xms jf_calls=N jf_total=Yms top=path(Nx=Yms), ...
_PERF_PATHS = {
    "/api/users", "/api/libraries",
    "/api/recent/movies", "/api/recent/episodes",
    "/api/activity", "/api/events/last-update",
    "/api/watch-summary/items",
}

def _perf_record_jf_call(path, dur):
    try:
        calls = getattr(g, "_jf_calls", None)
    except RuntimeError:
        return
    if calls is not None:
        calls.append((path, dur))

_jf_set_request_hook(_perf_record_jf_call)

@app.before_request
def _perf_start():
    if request.path in _PERF_PATHS:
        g._jf_calls = []
        g._req_t0 = time.monotonic()

@app.after_request
def _perf_log(resp):
    t0 = getattr(g, "_req_t0", None)
    if t0 is None:
        return resp
    calls = getattr(g, "_jf_calls", []) or []
    total_ms = int((time.monotonic() - t0) * 1000)
    jf_total_ms = int(sum(d for _, d in calls) * 1000)
    agg = {}
    for p, d in calls:
        slot = agg.setdefault(p, [0, 0.0])
        slot[0] += 1
        slot[1] += d
    parts = sorted(agg.items(), key=lambda x: -x[1][1])[:5]
    breakdown = ", ".join(f"{p}({n}x={int(t*1000)}ms)" for p, (n, t) in parts) or "-"
    print(f"[PERF] {request.path} {resp.status_code} total={total_ms}ms "
          f"jf_calls={len(calls)} jf_total={jf_total_ms}ms top={breakdown}",
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

_ID_RE = re.compile(r'^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|\d{1,20})$')
_USERNAME_RE = re.compile(r'^[a-zA-Z0-9_\-\.@]+$')
def vid(v):
    """Validate a Jellyfin Item ID (32-char hex, with or without dashes) or a legacy numeric ID from a URL path; abort 400 if invalid."""
    if not v or not _ID_RE.match(str(v)):
        abort(400)
    return str(v)

def is_setup_needed():
    admin = get_admin()
    if not (admin and admin.get("username") and admin.get("password_hash")):
        return True
    if not get_jellyfin_api_key():
        return True
    return False

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
        resp["jellyfin_url_from_env"] = os.environ.get("JELLYFIN_URL", "")
        resp["jellyfin_api_key_set"] = bool(os.environ.get("JELLYFIN_API_KEY"))
        resp["webhook_secret_set"] = bool(os.environ.get("JELLYFIN_WEBHOOK_SECRET"))
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
    jellyfin_url = (data.get("jellyfin_url") or "").strip()
    jellyfin_api_key = (data.get("jellyfin_api_key") or "").strip()
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
    if not os.environ.get("JELLYFIN_API_KEY") and not jellyfin_api_key:
        return jsonify({"success": False, "error": "Jellyfin API key is required"}), 400
    if not os.environ.get("JELLYFIN_WEBHOOK_SECRET") and not webhook_secret:
        return jsonify({"success": False, "error": "Webhook secret is required"}), 400
    if webhook_secret and len(webhook_secret) < 16:
        return jsonify({"success": False, "error": "Webhook secret must be at least 16 characters"}), 400
    salt = bcrypt.gensalt()
    pwd_hash = bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")
    cfg = load_config()
    cfg["admin"] = {"username": username, "password_hash": pwd_hash}
    if jellyfin_url: cfg["jellyfin_url"] = jellyfin_url
    if jellyfin_api_key: cfg["jellyfin_api_key"] = jellyfin_api_key
    if webhook_secret: cfg["webhook_secret"] = webhook_secret
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

@app.route("/api/webhook/jellyfin", methods=["POST"])
@csrf.exempt
@limiter.limit("120 per minute")
def api_webhook_jellyfin():
    secret = get_webhook_secret()
    if not secret:
        # Webhook is disabled until a secret is configured.
        return jsonify({"error": "Webhook not configured"}), 503
    incoming = request.headers.get("X-Webhook-Secret") or request.args.get("secret", "")
    if not hmac.compare_digest(incoming, secret):
        return jsonify({"error": "Unauthorized"}), 401

    # force=True parses the body as JSON regardless of Content-Type header —
    # some Jellyfin webhook plugin builds send the JSON body with text/plain.
    data = request.get_json(force=True, silent=True) or {}

    notification_type = data.get("NotificationType", "")
    # PlaybackStop: only count when playback reached Jellyfin's completion threshold.
    # UserDataSaved: covers the manual "Mark as Played" toggle in Jellyfin (Played=true).
    if notification_type == "PlaybackStop":
        if not data.get("PlayedToCompletion"):
            return jsonify({"ok": True, "skipped": "not_played_to_completion"})
    elif notification_type == "UserDataSaved":
        played_field = data.get("Played")
        if isinstance(played_field, str):
            played_field = played_field.strip().lower() == "true"
        if not played_field:
            return jsonify({"ok": True, "skipped": "user_data_saved_not_played"})
    else:
        return jsonify({"ok": True, "skipped": notification_type})

    account_id = data.get("UserId")
    if not account_id: return jsonify({"error": "no UserId"}), 400
    account_id = str(account_id)

    raw_type = (data.get("ItemType") or "").lower()
    type_map = {"movie": "movie", "episode": "episode"}
    item_type = type_map.get(raw_type)
    if not item_type:
        return jsonify({"ok": True, "skipped": f"type:{raw_type}"})

    rating_key = str(data.get("ItemId") or "")
    providers_raw = {}
    if data.get("Provider_tmdb"): providers_raw["Tmdb"] = str(data["Provider_tmdb"])
    if data.get("Provider_tvdb"): providers_raw["Tvdb"] = str(data["Provider_tvdb"])
    if data.get("Provider_imdb"): providers_raw["Imdb"] = str(data["Provider_imdb"])
    providers = parse_jellyfin_providers(providers_raw)

    if not providers and not rating_key:
        return jsonify({"ok": True, "skipped": "no provider ids or item id"})

    # Build a meta dict compatible with _maybe_auto_delete
    meta = {
        "Name": data.get("Name", ""),
        "type": item_type,
        "librarySectionID": None,
        "SeriesId": data.get("SeriesId") or data.get("SeriesName") or "",
        "ProviderIds": providers_raw,
        "UserData": {"Played": True, "LastPlayedDate": data.get("UtcTimestamp") or data.get("Timestamp")},
    }

    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    stored = 0
    for ptype, pid in providers.items():
        db.execute("""INSERT INTO watch_events (account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key)
            VALUES (?, ?, ?, ?, 'play', ?, ?)
            ON CONFLICT(account_id, provider_type, provider_id, item_type)
            DO UPDATE SET event_type=excluded.event_type, updated_at=excluded.updated_at, rating_key=excluded.rating_key""",
            (account_id, ptype.lower(), str(pid), item_type, now, rating_key))
        stored += 1
    if stored == 0 and rating_key:
        db.execute("""INSERT INTO watch_events (account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key)
            VALUES (?, 'ratingkey', ?, ?, 'play', ?, ?)
            ON CONFLICT(account_id, provider_type, provider_id, item_type)
            DO UPDATE SET event_type=excluded.event_type, updated_at=excluded.updated_at, rating_key=excluded.rating_key""",
            (account_id, rating_key, item_type, now, rating_key))
        stored = 1
    db.commit()
    app.logger.info(f"Jellyfin webhook: account={account_id} type={item_type} providers={providers} item_id={rating_key}")
    cfg = load_config()
    if item_type in ("movie", "episode"):
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
    event_count = db.execute("SELECT COUNT(*) as cnt FROM watch_events").fetchone()["cnt"]
    play_count = db.execute("SELECT COUNT(*) as cnt FROM watch_events WHERE event_type='play'").fetchone()["cnt"]
    accounts = db.execute("SELECT account_id, COUNT(*) as cnt FROM watch_events WHERE event_type='play' GROUP BY account_id ORDER BY account_id").fetchall()
    last_row = db.execute("SELECT updated_at FROM watch_events ORDER BY updated_at DESC LIMIT 1").fetchone()
    db.close()
    return jsonify({
        "total_events": event_count,
        "play_events": play_count,
        "last_event_at": last_row["updated_at"] if last_row else None,
        "accounts": [{"account_id": r["account_id"], "play_events": r["cnt"]} for r in accounts]
    })

@app.route("/api/webhook-events/for-item/<rating_key>")
@login_required_api
def api_webhook_events_for_item(rating_key):
    rating_key = vid(rating_key)
    db = get_db()
    rows = db.execute(
        "SELECT account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key "
        "FROM watch_events WHERE rating_key=? ORDER BY updated_at DESC",
        (rating_key,)).fetchall()
    recent = db.execute(
        "SELECT account_id, item_type, rating_key, updated_at FROM watch_events "
        "ORDER BY updated_at DESC LIMIT 20").fetchall()
    db.close()
    name_by_id = {u["id"]: u["name"] for u in jellyfin_users()}
    return jsonify({
        "rating_key": rating_key,
        "events_for_item": [
            {"account_id": r["account_id"], "account_name": name_by_id.get(r["account_id"], "?"),
             "provider_type": r["provider_type"], "provider_id": r["provider_id"],
             "item_type": r["item_type"], "event_type": r["event_type"],
             "updated_at": r["updated_at"], "rating_key": r["rating_key"]}
            for r in rows
        ],
        "most_recent_overall": [
            {"account_id": r["account_id"], "account_name": name_by_id.get(r["account_id"], "?"),
             "item_type": r["item_type"], "rating_key": r["rating_key"], "updated_at": r["updated_at"]}
            for r in recent
        ]
    })

@app.route("/api/webhook-events", methods=["DELETE"])
@login_required_api
def api_webhook_events_clear():
    db = get_db()
    db.execute("DELETE FROM watch_events")
    db.commit(); db.close()
    return jsonify({"success": True})

@app.route("/api/item/<item_id>")
@login_required_api
def api_item_info(item_id):
    item_id = vid(item_id)
    i = jellyfin_get_item(item_id, {"fields": "ProviderIds"})
    if not i: return jsonify({"error": "not found"}), 404
    jf_type = i.get("Type", "")
    type_map = {"Series": ("tvshows", "Series"), "Movie": ("movies", "Movie"),
                "Season": ("tvshows", "Season"), "Episode": ("tvshows", "Episode")}
    ctype, itype = type_map.get(jf_type, ("", jf_type))
    return jsonify({
        "id": str(i.get("Id", item_id)),
        "name": i.get("Name", ""),
        "type": itype,
        "collectionType": ctype,
        "seriesId": str(i["SeriesId"]) if i.get("SeriesId") else None,
        "seriesName": i.get("SeriesName") or None
    })

@app.route("/api/users")
@login_required_api
def api_users(): return jsonify(jellyfin_users())

@app.route("/api/libraries")
@login_required_api
def api_libraries():
    sections = jellyfin_libraries()
    config = load_config()
    out = []
    for s in sections:
        ctype = (s.get("CollectionType") or "").lower()
        if ctype not in ("tvshows", "movies"): continue
        lid = str(s["Id"])
        out.append({"id": lid, "name": s.get("Name", ""), "type": ctype,
            "monitored": config.get("show_all_libraries", True) or lid in config.get("monitored_libraries", [])})
    return jsonify(out)

@app.route("/api/series")
@login_required_api
def api_series():
    pid = request.args.get("parentId", "")
    search = request.args.get("search", "")
    genre = request.args.get("genre", "").strip()
    try: page = max(1, int(request.args.get("page", "1")))
    except (ValueError, TypeError): page = 1
    ids = request.args.get("ids", "")
    if pid and not _ID_RE.match(pid): abort(400)

    if ids:
        id_list = [i.strip() for i in ids.split(",") if i.strip() and _ID_RE.match(i.strip())]
        items = []
        for rid in id_list:
            try:
                m = jellyfin_get_item(rid)
                items.append({"id": m["Id"], "name": m.get("Name", ""), "year": m.get("ProductionYear")})
            except Exception: continue
        return jsonify({"items": items, "totalCount": len(items), "page": 1, "pageSize": len(items), "isSearch": False})

    if not pid:
        return jsonify({"items": [], "totalCount": 0, "page": page, "pageSize": PAGE_SIZE, "isSearch": False})

    params = {"ParentId": pid, "IncludeItemTypes": "Series", "Recursive": "true",
              "SortBy": "SortName", "SortOrder": "Ascending",
              "Limit": 100 if search else PAGE_SIZE,
              "StartIndex": 0 if search else (page - 1) * PAGE_SIZE}
    if search: params["SearchTerm"] = search
    if genre: params["Genres"] = genre
    data = jellyfin_get("/Items", params)
    items = data.get("Items", [])
    total = data.get("TotalRecordCount", len(items))
    return jsonify({"items": [{"id": i["Id"], "name": i.get("Name", ""), "year": i.get("ProductionYear")} for i in items],
        "totalCount": total, "page": page, "pageSize": PAGE_SIZE, "isSearch": bool(search)})

@app.route("/api/movies")
@login_required_api
def api_movies():
    pid = request.args.get("parentId", "")
    search = request.args.get("search", "")
    genre = request.args.get("genre", "").strip()
    try: page = max(1, int(request.args.get("page", "1")))
    except (ValueError, TypeError): page = 1
    ids = request.args.get("ids", "")
    if pid and not _ID_RE.match(pid): abort(400)

    if ids:
        id_list = [i.strip() for i in ids.split(",") if i.strip() and _ID_RE.match(i.strip())]
        items = []
        for rid in id_list:
            try:
                m = jellyfin_get_item(rid)
                items.append({"id": m["Id"], "name": m.get("Name", ""), "year": m.get("ProductionYear")})
            except Exception: continue
        return jsonify({"items": items, "totalCount": len(items), "page": 1, "pageSize": len(items), "isSearch": False})

    if not pid:
        return jsonify({"items": [], "totalCount": 0, "page": page, "pageSize": PAGE_SIZE, "isSearch": False})

    params = {"ParentId": pid, "IncludeItemTypes": "Movie", "Recursive": "true",
              "SortBy": "SortName", "SortOrder": "Ascending",
              "Limit": 100 if search else PAGE_SIZE,
              "StartIndex": 0 if search else (page - 1) * PAGE_SIZE}
    if search: params["SearchTerm"] = search
    if genre: params["Genres"] = genre
    data = jellyfin_get("/Items", params)
    items = data.get("Items", [])
    total = data.get("TotalRecordCount", len(items))
    return jsonify({"items": [{"id": i["Id"], "name": i.get("Name", ""), "year": i.get("ProductionYear")} for i in items],
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
    for s in jellyfin_libraries():
        ctype = (s.get("CollectionType") or "").lower()
        if ctype not in ("tvshows", "movies"): continue
        lid = str(s["Id"])
        if not show_all and lid not in monitored: continue
        jf_type = "Movie" if ctype == "movies" else "Series"
        kind = "movie" if ctype == "movies" else "series"
        try:
            data = jellyfin_get("/Items", {
                "ParentId": lid, "IncludeItemTypes": jf_type, "Recursive": "true",
                "SearchTerm": q, "Limit": 25,
                "SortBy": "SortName", "SortOrder": "Ascending",
            })
        except Exception:
            continue
        for i in data.get("Items", []):
            results.append({"id": i["Id"], "name": i.get("Name", ""),
                            "year": i.get("ProductionYear"),
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
    if not pid: return jsonify({})
    if not _ID_RE.match(pid): abort(400)

    accounts = jellyfin_users()
    if not accounts: return jsonify({})
    all_account_ids = {a["id"] for a in accounts}
    owner_id = jellyfin_admin_id()

    jf_type = "Movie" if item_type == "movies" else "Series"
    data = jellyfin_get("/Items", {"ParentId": pid, "IncludeItemTypes": jf_type,
                                   "Recursive": "true", "fields": "ProviderIds",
                                   "Limit": 10000})
    all_items = data.get("Items", [])
    if not all_items: return jsonify({})

    db = get_db()
    item_assignments, provider_assignments = _load_assignment_maps(db)

    def resolve_target(providers, item_id):
        return _resolve_target(providers, item_id, item_assignments, provider_assignments, all_account_ids)

    has_pwe = pwe_has_data(db)
    result = {}

    if has_pwe:
        if item_type == "movies":
            played = pwe_get_played(db, list(all_account_ids), ["movie"])
            played_rk = pwe_get_played_by_ratingkey(db, list(all_account_ids), ["movie"])
            for i in all_items:
                iid = str(i["Id"])
                providers = parse_jellyfin_providers(i.get("ProviderIds") or {})
                target_ids = resolve_target(providers, iid)
                if not target_ids:
                    result[iid] = {"watched": 0, "total": 0, "watcher_ids": []}
                    continue
                watched_count = 0; watcher_ids = []
                rk_watchers = played_rk.get(iid, set())
                for aid in target_ids:
                    saw_it = (bool(providers) and is_item_watched_pwe(providers, "movie", {aid}, played)) or (
                        aid in rk_watchers)
                    if saw_it:
                        watched_count += 1; watcher_ids.append(aid)
                result[iid] = {"watched": watched_count, "total": len(target_ids), "watcher_ids": watcher_ids}
        else:
            # TV shows: aggregate episode-level webhook events
            ep_data = jellyfin_get("/Items", {"ParentId": pid, "IncludeItemTypes": "Episode",
                                              "Recursive": "true", "fields": "ProviderIds,SeriesId",
                                              "Limit": 50000})
            show_eps = {}  # {show_id: [(providers, ep_id), ...]}
            for ep in ep_data.get("Items", []):
                show_id = str(ep.get("SeriesId", ""))
                ep_id = str(ep.get("Id", ""))
                providers = parse_jellyfin_providers(ep.get("ProviderIds") or {})
                if show_id and (providers or ep_id):
                    show_eps.setdefault(show_id, []).append((providers, ep_id))
            ep_played = pwe_get_played(db, list(all_account_ids), ["episode"])
            ep_played_rk = pwe_get_played_by_ratingkey(db, list(all_account_ids), ["episode"])

            for i in all_items:
                iid = str(i["Id"])
                show_providers = parse_jellyfin_providers(i.get("ProviderIds") or {})
                target_ids = resolve_target(show_providers, iid)
                eps = show_eps.get(iid, [])
                if not target_ids or not eps:
                    result[iid] = {"watched": 0, "total": len(target_ids), "watcher_ids": []}
                    continue
                watched_count = 0; watcher_ids = []
                for aid in target_ids:
                    saw_all = all(
                        (bool(p) and is_item_watched_pwe(p, "episode", {aid}, ep_played)) or
                        (rk and aid in ep_played_rk.get(rk, set()))
                        for p, rk in eps
                    )
                    if saw_all:
                        watched_count += 1; watcher_ids.append(aid)
                result[iid] = {"watched": watched_count, "total": len(target_ids), "watcher_ids": watcher_ids}
    else:
        # No webhook data — return zero counts (Jellyfin UserData requires per-user requests)
        for i in all_items:
            iid = str(i["Id"])
            providers = parse_jellyfin_providers(i.get("ProviderIds") or {})
            target_ids = resolve_target(providers, iid)
            result[iid] = {"watched": 0, "total": len(target_ids), "watcher_ids": []}

    db.close()
    return jsonify(result)

@app.route("/api/genres")
@login_required_api
def api_genres():
    pid = request.args.get("parentId", "")
    if not pid: return jsonify([])
    if not _ID_RE.match(pid): abort(400)
    try:
        mc = jellyfin_get("/Genres", {"ParentId": pid, "Limit": 1000})
        genres = sorted(set(g.get("Name", "") for g in mc.get("Items", []) if g.get("Name")))
        return jsonify(genres)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/seasons/<series_id>")
@login_required_api
def api_seasons(series_id):
    series_id = vid(series_id)
    seasons_raw = jellyfin_items({
        "ParentId": series_id, "IncludeItemTypes": "Season",
        "SortBy": "IndexNumber", "fields": "ChildCount"
    })
    if not seasons_raw: return jsonify([])

    accounts = jellyfin_users()
    assigned_ids = get_assigned_ids(series_id)
    target_accounts = accounts if assigned_ids is None else [a for a in accounts if a["id"] in assigned_ids]
    if not target_accounts: target_accounts = accounts

    db = get_db()
    has_pwe = pwe_has_data(db)

    if has_pwe:
        target_ids = [a["id"] for a in target_accounts]
        played = pwe_get_played(db, target_ids, ["episode"])
        played_rk = pwe_get_played_by_ratingkey(db, target_ids, ["episode"])
        db.close()

        try:
            all_eps = jellyfin_items({
                "ParentId": series_id, "Recursive": "true",
                "IncludeItemTypes": "Episode", "fields": "ProviderIds",
                "SortBy": "IndexNumber"
            })
        except Exception:
            all_eps = []

        season_eps = {}
        for ep in all_eps:
            parent_id = str(ep.get("SeasonId") or ep.get("ParentId") or "")
            if parent_id:
                season_eps.setdefault(parent_id, []).append(
                    (str(ep["Id"]), parse_jellyfin_providers(ep.get("ProviderIds") or {})))

        result = []
        for s in seasons_raw:
            sid = str(s["Id"])
            total = s.get("ChildCount") or 0
            eps = season_eps.get(sid, [])
            if not total and eps: total = len(eps)
            completed_users, per_user = 0, []
            for acc in target_accounts:
                if eps:
                    t = len(eps)
                    played_count = sum(
                        1 for ep_id, providers in eps
                        if (providers and is_item_watched_pwe(providers, "episode", {acc["id"]}, played))
                        or (ep_id and acc["id"] in played_rk.get(ep_id, set())))
                    completed = t > 0 and played_count >= t
                else:
                    played_count, t, completed = 0, total, False
                if completed: completed_users += 1
                per_user.append({"userId": acc["id"], "userName": acc["name"],
                                 "playedCount": played_count, "totalCount": t, "completed": completed})
            result.append({"id": sid, "name": s.get("Name", ""), "indexNumber": s.get("IndexNumber", 0),
                           "totalEpisodes": total, "userProgress": per_user,
                           "completedUsers": completed_users, "totalAssignedUsers": len(target_accounts)})
        return jsonify(result)

    db.close()
    # No webhook data: query Jellyfin UserData per user per season
    admin_id = jellyfin_admin_id()
    result = []
    for s in seasons_raw:
        sid = str(s["Id"])
        total = s.get("ChildCount") or 0
        completed_users, per_user = 0, []
        for acc in target_accounts:
            try:
                played_eps = jellyfin_items({
                    "ParentId": sid, "IncludeItemTypes": "Episode",
                    "IsPlayed": "true", "userId": acc["id"]
                })
                played_count = len(played_eps)
            except Exception:
                played_count = 0
            completed = total > 0 and played_count >= total
            if completed: completed_users += 1
            per_user.append({"userId": acc["id"], "userName": acc["name"],
                             "playedCount": played_count, "totalCount": total, "completed": completed})
        result.append({"id": sid, "name": s.get("Name", ""), "indexNumber": s.get("IndexNumber", 0),
                       "totalEpisodes": total, "userProgress": per_user,
                       "completedUsers": completed_users, "totalAssignedUsers": len(target_accounts)})
    return jsonify(result)

@app.route("/api/season-watch-status/<series_id>/<season_id>")
@login_required_api
def api_season_watch_status(series_id, season_id):
    series_id = vid(series_id); season_id = vid(season_id)
    accounts = jellyfin_users()

    db = get_db()
    has_pwe = pwe_has_data(db)

    episodes_raw = jellyfin_items({
        "ParentId": season_id, "IncludeItemTypes": "Episode",
        "fields": "ProviderIds", "SortBy": "IndexNumber"
    })

    if has_pwe:
        all_ids = [a["id"] for a in accounts]
        played = pwe_get_played(db, all_ids, ["episode"])
        played_ts = pwe_get_played_with_ts(db, all_ids, ["episode"])
        played_rk = pwe_get_played_by_ratingkey(db, all_ids, ["episode"])
        db.close()

        result = []
        for ep in episodes_raw:
            eid = str(ep["Id"])
            providers = parse_jellyfin_providers(ep.get("ProviderIds") or {})
            rk_watchers = played_rk.get(eid, set())
            users = []
            for acc in accounts:
                is_played = (bool(providers) and is_item_watched_pwe(providers, "episode", {acc["id"]}, played)) or (acc["id"] in rk_watchers)
                last_ts = get_last_played_pwe(providers, "episode", acc["id"], played_ts) if providers else None
                users.append({"userId": acc["id"], "userName": acc["name"],
                              "played": is_played, "playCount": 1 if is_played else 0,
                              "lastPlayedDate": last_ts, "playedPercentage": 100.0 if is_played else 0.0})
            result.append({"id": eid, "name": ep.get("Name", ""),
                           "indexNumber": ep.get("IndexNumber", 0),
                           "runTimeTicks": ep.get("RunTimeTicks") or 0,
                           "users": users})
        return jsonify(result)

    db.close()
    # No webhook data: query Jellyfin UserData per user
    result = []
    for ep in episodes_raw:
        eid = str(ep["Id"])
        runtime_ticks = ep.get("RunTimeTicks") or 0
        users = []
        for acc in accounts:
            try:
                ud_data = jellyfin_get_item(eid, {"fields": "UserData", "userId": acc["id"]})
                ud = ud_data.get("UserData") or {}
                is_played = bool(ud.get("Played", False))
                play_count = ud.get("PlayCount") or (1 if is_played else 0)
                last_ts = ud.get("LastPlayedDate")
                pos_ticks = ud.get("PlaybackPositionTicks") or 0
                pct = 100.0 if is_played else (round(pos_ticks / runtime_ticks * 100, 1) if runtime_ticks else 0)
            except Exception:
                is_played, play_count, last_ts, pct = False, 0, None, 0.0
            users.append({"userId": acc["id"], "userName": acc["name"],
                          "played": is_played, "playCount": play_count,
                          "lastPlayedDate": last_ts, "playedPercentage": pct})
        result.append({"id": eid, "name": ep.get("Name", ""),
                       "indexNumber": ep.get("IndexNumber", 0),
                       "runTimeTicks": runtime_ticks, "users": users})
    return jsonify(result)

@app.route("/api/watch-status/<item_id>")
@login_required_api
def api_watch_status(item_id):
    item_id = vid(item_id)
    accounts = jellyfin_users()

    db = get_db()
    has_pwe = pwe_has_data(db)

    item_data = jellyfin_get_item(item_id, {"fields": "ProviderIds"})
    raw_type = item_data.get("Type", "Movie")  # "Movie", "Episode", "Series"
    providers = parse_jellyfin_providers(item_data.get("ProviderIds") or {})
    all_ids = [a["id"] for a in accounts]

    if has_pwe:
        if raw_type == "Series":
            try:
                all_eps = jellyfin_items({
                    "ParentId": item_id, "Recursive": "true",
                    "IncludeItemTypes": "Episode", "fields": "ProviderIds"
                })
                eps = [(str(e.get("Id") or ""), parse_jellyfin_providers(e.get("ProviderIds") or {})) for e in all_eps]
                eps = [(eid, p) for eid, p in eps if eid or p]
            except Exception:
                eps = []
            ep_played = pwe_get_played(db, all_ids, ["episode"])
            ep_played_ts = pwe_get_played_with_ts(db, all_ids, ["episode"])
            ep_played_rk = pwe_get_played_by_ratingkey(db, all_ids, ["episode"])
            db.close()
            out = []
            for acc in accounts:
                is_played = bool(eps) and all(
                    (bool(p) and is_item_watched_pwe(p, "episode", {acc["id"]}, ep_played))
                    or (eid and acc["id"] in ep_played_rk.get(eid, set()))
                    for eid, p in eps)
                last_ts = None
                if is_played and eps:
                    for _, p in eps:
                        if not p: continue
                        t = get_last_played_pwe(p, "episode", acc["id"], ep_played_ts)
                        if t and (last_ts is None or t > last_ts): last_ts = t
                out.append({"userId": acc["id"], "userName": acc["name"], "played": is_played,
                            "playCount": 1 if is_played else 0, "lastPlayedDate": last_ts,
                            "playedPercentage": 100.0 if is_played else 0.0})
        else:
            ws_itype = "episode" if raw_type == "Episode" else "movie"
            played = pwe_get_played(db, all_ids, [ws_itype])
            played_ts = pwe_get_played_with_ts(db, all_ids, [ws_itype])
            played_rk = pwe_get_played_by_ratingkey(db, all_ids, [ws_itype])
            db.close()
            rk_watchers = played_rk.get(item_id, set())
            out = []
            for acc in accounts:
                is_played = (bool(providers) and is_item_watched_pwe(providers, ws_itype, {acc["id"]}, played)) or (acc["id"] in rk_watchers)
                last_ts = get_last_played_pwe(providers, ws_itype, acc["id"], played_ts) if providers else None
                out.append({"userId": acc["id"], "userName": acc["name"], "played": is_played,
                            "playCount": 1 if is_played else 0, "lastPlayedDate": last_ts,
                            "playedPercentage": 100.0 if is_played else 0.0})
        return jsonify(out)

    db.close()
    # No webhook data: query Jellyfin UserData per user
    out = []
    for acc in accounts:
        try:
            ud_data = jellyfin_get_item(item_id, {"fields": "UserData", "userId": acc["id"]})
            ud = ud_data.get("UserData") or {}
            is_played = bool(ud.get("Played", False))
            play_count = ud.get("PlayCount") or (1 if is_played else 0)
            last_ts = ud.get("LastPlayedDate")
            pos_ticks = ud.get("PlaybackPositionTicks") or 0
            rt = ud_data.get("RunTimeTicks") or 1
            pct = 100.0 if is_played else round(min(pos_ticks / rt * 100, 99.9), 1)
        except Exception:
            is_played, play_count, last_ts, pct = False, 0, None, 0.0
        out.append({"userId": acc["id"], "userName": acc["name"], "played": is_played,
                    "playCount": play_count, "lastPlayedDate": last_ts, "playedPercentage": pct})
    return jsonify(out)

def _admin_recent_items(admin_id, include_type, fields, monitored, show_all):
    """Jellyfin admin IsPlayed history, scoped to monitored libraries if needed.

    Filtering by item.ParentId after the fact doesn't work because
    /Library/MediaFolders can return view IDs that don't match the
    CollectionFolder IDs items carry as ParentId. Scoping the query with
    the parentId query parameter delegates hierarchy resolution to
    Jellyfin, which always gets it right.
    """
    base = {
        "userId": admin_id, "IsPlayed": "true", "Recursive": "true",
        "IncludeItemTypes": include_type, "SortBy": "DatePlayed",
        "SortOrder": "Descending", "Limit": 20, "fields": fields,
    }
    if show_all or not monitored:
        try:
            return jellyfin_items(base)
        except Exception:
            return []
    seen = {}
    for lib_id in monitored:
        try:
            items = jellyfin_items({**base, "parentId": lib_id})
        except Exception:
            continue
        for it in items:
            rid = str(it.get("Id") or "")
            if rid:
                seen.setdefault(rid, it)
    return list(seen.values())

def _bulk_items_by_ids(rating_keys, include_type, fields, monitored, show_all):
    """Batch-fetch /Items?Ids=... for a list of rating keys.

    Returns dict {rating_key: item}, restricted to items under a monitored
    library when show_all is False. Replaces the old per-item
    jellyfin_get_item loop and the per-library _filter_rks_by_library scan
    with one combined query per library (or one total when show_all=True).
    """
    rks = [str(rk) for rk in rating_keys if rk]
    if not rks:
        return {}
    base = {
        "Ids": ",".join(rks),
        "IncludeItemTypes": include_type,
        "Recursive": "true",
        "fields": fields,
        "Limit": len(rks),
    }
    out = {}
    parents = [None] if (show_all or not monitored) else list(monitored)
    for parent in parents:
        params = base if parent is None else {**base, "parentId": parent}
        try:
            items = jellyfin_items(params)
        except Exception:
            continue
        for it in items:
            rid = str(it.get("Id") or "")
            if rid:
                out.setdefault(rid, it)
    return out

@app.route("/api/recent/movies")
@login_required_api
def api_recent_movies():
    cfg = load_config()
    monitored = [str(x) for x in cfg.get("monitored_libraries", [])]
    show_all = cfg.get("show_all_libraries", True)
    admin_id = jellyfin_admin_id()
    result = {}

    # Admin's native watch history from Jellyfin
    if admin_id:
        for i in _admin_recent_items(admin_id, "Movie",
                                     "ProviderIds,ParentId,UserData",
                                     monitored, show_all):
            rid = str(i.get("Id") or "")
            if not rid: continue
            lv = (i.get("UserData") or {}).get("LastPlayedDate")
            if not lv: continue
            result[rid] = {"id": rid, "name": i.get("Name", ""),
                "year": i.get("ProductionYear"),
                "imageUrl": f"/api/image/{rid}?type=Primary&maxWidth=200",
                "lastPlayedDate": lv}

    # Merge all-user webhook events
    db = get_db()
    rows = db.execute("""
        SELECT rating_key, MAX(updated_at) as latest_at
        FROM watch_events
        WHERE event_type='play' AND item_type='movie' AND rating_key != ''
        GROUP BY rating_key ORDER BY latest_at DESC LIMIT 60
    """).fetchall()
    db.close()

    ts_by_rk = {r["rating_key"]: r["latest_at"] for r in rows}
    needed = [rid for rid, ts in ts_by_rk.items()
              if (result.get(rid, {}).get("lastPlayedDate") or "") < ts]

    items = _bulk_items_by_ids(needed, "Movie", "ProviderIds", monitored, show_all)
    for rid, i in items.items():
        ts = ts_by_rk.get(rid)
        if not ts: continue
        result[rid] = {"id": rid, "name": i.get("Name", ""),
            "year": i.get("ProductionYear"),
            "imageUrl": f"/api/image/{rid}?type=Primary&maxWidth=200",
            "lastPlayedDate": ts}

    out = sorted(result.values(), key=lambda x: x.get("lastPlayedDate") or "", reverse=True)
    limit = request.args.get("limit", 10, type=int)
    return jsonify(out[:limit])

@app.route("/api/recent/episodes")
@login_required_api
def api_recent_episodes():
    cfg = load_config()
    monitored = [str(x) for x in cfg.get("monitored_libraries", [])]
    show_all = cfg.get("show_all_libraries", True)
    admin_id = jellyfin_admin_id()
    result = {}

    # Admin's native watch history from Jellyfin
    if admin_id:
        for i in _admin_recent_items(admin_id, "Episode",
                                     "ProviderIds,UserData",
                                     monitored, show_all):
            rid = str(i.get("Id") or "")
            if not rid: continue
            lv = (i.get("UserData") or {}).get("LastPlayedDate")
            if not lv: continue
            series_id = str(i.get("SeriesId") or "")
            result[rid] = {"id": rid, "name": i.get("Name", ""),
                "seriesName": i.get("SeriesName", ""),
                "seasonName": i.get("SeasonName", ""),
                "episodeNumber": i.get("IndexNumber"),
                "imageUrl": f"/api/image/{series_id or rid}?type=Primary&maxWidth=200",
                "lastPlayedDate": lv, "seriesId": series_id}

    # Merge all-user webhook events
    db = get_db()
    rows = db.execute("""
        SELECT rating_key, MAX(updated_at) as latest_at
        FROM watch_events
        WHERE event_type='play' AND item_type='episode' AND rating_key != ''
        GROUP BY rating_key ORDER BY latest_at DESC LIMIT 60
    """).fetchall()
    db.close()

    ts_by_rk = {r["rating_key"]: r["latest_at"] for r in rows}
    needed = [rid for rid, ts in ts_by_rk.items()
              if (result.get(rid, {}).get("lastPlayedDate") or "") < ts]

    items = _bulk_items_by_ids(needed, "Episode", "ProviderIds", monitored, show_all)
    for rid, i in items.items():
        ts = ts_by_rk.get(rid)
        if not ts: continue
        series_id = str(i.get("SeriesId") or "")
        result[rid] = {"id": rid, "name": i.get("Name", ""),
            "seriesName": i.get("SeriesName", ""),
            "seasonName": i.get("SeasonName", ""),
            "episodeNumber": i.get("IndexNumber"),
            "imageUrl": f"/api/image/{series_id or rid}?type=Primary&maxWidth=200",
            "lastPlayedDate": ts, "seriesId": series_id}

    out = sorted(result.values(), key=lambda x: x.get("lastPlayedDate") or "", reverse=True)
    limit = request.args.get("limit", 10, type=int)
    return jsonify(out[:limit])

@app.route("/api/admin/backfill-history", methods=["POST"])
@limiter.limit("2 per hour")
@login_required_api
def api_backfill_history():
    """Import historical play events from Jellyfin's played history for all users."""
    accounts = jellyfin_all_users()
    if not accounts:
        return jsonify({"ok": False, "error": "no accounts"}), 400

    db = get_db()
    imported = 0
    total_history = 0

    for acc in accounts:
        uid = acc["id"]
        try:
            items = jellyfin_items({
                "userId": uid, "IsPlayed": "true", "Recursive": "true",
                "IncludeItemTypes": "Movie,Episode",
                "fields": "ProviderIds,UserData", "Limit": 10000
            })
        except Exception:
            continue

        for item in items:
            raw_type = item.get("Type", "")
            if raw_type not in ("Movie", "Episode"):
                continue
            rk = str(item.get("Id", ""))
            providers = parse_jellyfin_providers(item.get("ProviderIds") or {})
            if not rk or not providers:
                continue
            item_type = "movie" if raw_type == "Movie" else "episode"
            ud = item.get("UserData") or {}
            iso_ts = ud.get("LastPlayedDate") or datetime.now(timezone.utc).isoformat()
            total_history += 1
            for ptype, pid in providers.items():
                cur = db.execute(
                    """INSERT OR IGNORE INTO watch_events
                       (account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key)
                       VALUES (?, ?, ?, ?, 'play', ?, ?)""",
                    (uid, ptype.lower(), str(pid), item_type, iso_ts, rk)
                )
                imported += cur.rowcount

    db.commit()
    db.close()
    return jsonify({"ok": True, "imported": imported, "total_history": total_history})

@app.route("/api/watch-summary/items")
@login_required_api
def api_watch_summary_items():
    raw_movie_ids = [x for x in (request.args.get("movieIds", "") or "").split(",") if x]
    raw_ep_ids   = [x for x in (request.args.get("episodeIds", "") or "").split(",") if x]
    movie_ids = [i for i in raw_movie_ids if _ID_RE.match(i)][:20]
    ep_ids    = [i for i in raw_ep_ids    if _ID_RE.match(i)][:20]
    if not movie_ids and not ep_ids:
        return jsonify({})

    accounts = jellyfin_users()
    if not accounts:
        return jsonify({})
    all_account_ids = {a["id"] for a in accounts}

    db = get_db()
    item_assignments, provider_assignments = _load_assignment_maps(db)
    has_pwe = pwe_has_data(db)

    def resolve_target(providers, item_id):
        return _resolve_target(providers, item_id, item_assignments, provider_assignments, all_account_ids)

    result = {}

    if not has_pwe:
        db.close()
        # No webhook data: return 0 counts (no reliable per-user data without webhooks)
        for iid in movie_ids + ep_ids:
            result[iid] = {"watched": 0, "total": 0, "watcher_ids": []}
        return jsonify(result)

    played = pwe_get_played(db, list(all_account_ids), ["movie", "episode"])
    db.close()

    # One batched call for every movie/episode id, plus one for the
    # distinct series ids the episodes belong to.
    items_by_id = {}
    all_ids = list({*movie_ids, *ep_ids})
    if all_ids:
        try:
            for it in jellyfin_items({
                "Ids": ",".join(all_ids),
                "fields": "ProviderIds",
                "Recursive": "true",
                "Limit": len(all_ids),
            }):
                iid = str(it.get("Id") or "")
                if iid: items_by_id[iid] = it
        except Exception:
            pass

    series_ids = list({str(items_by_id.get(eid, {}).get("SeriesId") or "")
                       for eid in ep_ids if items_by_id.get(eid, {}).get("SeriesId")})
    series_providers = {}
    if series_ids:
        try:
            for it in jellyfin_items({
                "Ids": ",".join(series_ids),
                "fields": "ProviderIds",
                "Recursive": "true",
                "Limit": len(series_ids),
            }):
                sid = str(it.get("Id") or "")
                if sid:
                    series_providers[sid] = parse_jellyfin_providers(it.get("ProviderIds") or {})
        except Exception:
            pass

    for iid in movie_ids:
        it = items_by_id.get(iid)
        if not it:
            result[iid] = {"watched": 0, "total": 0, "watcher_ids": []}
            continue
        providers = parse_jellyfin_providers(it.get("ProviderIds") or {})
        target_ids = resolve_target(providers, iid)
        watched_count = 0; watcher_ids = []
        for aid in target_ids:
            if bool(providers) and is_item_watched_pwe(providers, "movie", {aid}, played):
                watched_count += 1; watcher_ids.append(aid)
        result[iid] = {"watched": watched_count, "total": len(target_ids), "watcher_ids": watcher_ids}

    for iid in ep_ids:
        it = items_by_id.get(iid)
        if not it:
            result[iid] = {"watched": 0, "total": 0, "watcher_ids": []}
            continue
        providers = parse_jellyfin_providers(it.get("ProviderIds") or {})
        series_id = str(it.get("SeriesId") or "")
        show_providers = series_providers.get(series_id, {}) if series_id else {}
        target_ids = resolve_target(show_providers, series_id)
        watched_count = 0; watcher_ids = []
        for aid in target_ids:
            saw_it = is_item_watched_pwe(providers, "episode", {aid}, played) if providers else False
            if saw_it:
                watched_count += 1; watcher_ids.append(aid)
        result[iid] = {"watched": watched_count, "total": len(target_ids), "watcher_ids": watcher_ids}

    return jsonify(result)

@app.route("/api/activity")
@login_required_api
def api_activity():
    try:
        sessions = jellyfin_get("/Sessions")
    except Exception:
        return jsonify([])
    out = []
    for i in sessions:
        npi = i.get("NowPlayingItem") or {}
        if not npi: continue
        rid = str(npi.get("Id", ""))
        ps = i.get("PlayState") or {}
        pos_ticks = ps.get("PositionTicks") or 0
        rt_ticks = npi.get("RunTimeTicks") or 1
        progress = round(min(pos_ticks / rt_ticks * 100, 100), 1)
        play_method = (ps.get("PlayMethod") or "").lower()
        stream_type = "Direct Play" if play_method == "directplay" else ("Transcode" if play_method == "transcode" else "")
        raw_type = npi.get("Type", "")
        item_type = raw_type.lower()
        series_id = str(npi.get("SeriesId") or "")
        out.append({
            "id": str(i.get("Id", rid)),
            "type": item_type,
            "title": npi.get("Name", ""),
            "seriesName": npi.get("SeriesName") or None,
            "seasonName": npi.get("SeasonName") or None,
            "episodeNumber": npi.get("IndexNumber"),
            "imageUrl": f"/api/image/{series_id or rid}?type=Primary&maxWidth=300" if rid else None,
            "progress": progress,
            "viewOffset": pos_ticks // 10000,
            "duration": rt_ticks // 10000,
            "user": i.get("UserName", "Unknown"),
            "player": i.get("DeviceName", ""),
            "streamType": stream_type,
            "bandwidth": None,
            "ratingKey": rid,
            "grandparentRatingKey": series_id,
            "parentRatingKey": str(npi.get("SeasonId") or ""),
            "librarySectionID": str(npi.get("ParentId") or ""),
        })
    return jsonify(out)

@app.route("/api/events/last-update")
@login_required_api
def api_events_last_update():
    db = get_db()
    row = db.execute("SELECT MAX(updated_at) as last_at FROM watch_events").fetchone()
    db.close()
    return jsonify({"last_at": row["last_at"] if row else None})

@app.route("/api/accounts")
@login_required_api
def api_accounts_all():
    cfg = load_config()
    hidden = set(cfg.get("hidden_accounts", []))
    all_accs = jellyfin_all_users()
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
        except Exception as e:
            failed_ids.append({"id": item_id, "error": str(e)})
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
        data = jellyfin_get_item(series_id, {"fields": "ParentId"})
        library_id = str(data.get("ParentId") or "")
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
        enabled_at = now_ts
        try:
            admin_id = jellyfin_admin_id()
            params = {"fields": "UserData"}
            if admin_id:
                params["userId"] = admin_id
            item = jellyfin_get_item(series_id, params)
            ud = item.get("UserData") or {}
            lva = ud.get("LastPlayedDate")
            played = ud.get("PlayCount") or (1 if ud.get("Played") else 0)
            if lva and played > 0:
                earliest_event = db.execute(
                    "SELECT MIN(updated_at) as ts FROM watch_events WHERE rating_key=? AND event_type='play'",
                    (series_id,)).fetchone()
                earliest_ts = earliest_event["ts"] if earliest_event and earliest_event["ts"] else None
                watch_dt = datetime.fromisoformat(lva.replace("Z", "+00:00"))
                if earliest_ts:
                    try:
                        event_dt = datetime.fromisoformat(earliest_ts.replace("Z", "+00:00"))
                        watch_dt = min(watch_dt, event_dt)
                    except Exception:
                        pass
                enabled_at = (watch_dt - timedelta(seconds=60)).isoformat()
        except Exception:
            pass
        db.execute("""INSERT INTO auto_delete_overrides (scope, scope_id, enabled, enabled_at)
                      VALUES ('series', ?, 1, ?)
                      ON CONFLICT(scope, scope_id) DO UPDATE SET enabled=1, enabled_at=excluded.enabled_at""",
                   (series_id, enabled_at))
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
        data = jellyfin_get_item(movie_id, {"fields": "ParentId"})
        library_id = str(data.get("ParentId") or "")
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
        enabled_at = now_ts
        try:
            admin_id = jellyfin_admin_id()
            params = {"fields": "UserData"}
            if admin_id:
                params["userId"] = admin_id
            item = jellyfin_get_item(movie_id, params)
            ud = item.get("UserData") or {}
            lva = ud.get("LastPlayedDate")
            played = ud.get("PlayCount") or (1 if ud.get("Played") else 0)
            if lva and played > 0:
                earliest_event = db.execute(
                    "SELECT MIN(updated_at) as ts FROM watch_events WHERE rating_key=? AND event_type='play'",
                    (movie_id,)).fetchone()
                earliest_ts = earliest_event["ts"] if earliest_event and earliest_event["ts"] else None
                watch_dt = datetime.fromisoformat(lva.replace("Z", "+00:00"))
                if earliest_ts:
                    try:
                        event_dt = datetime.fromisoformat(earliest_ts.replace("Z", "+00:00"))
                        watch_dt = min(watch_dt, event_dt)
                    except Exception:
                        pass
                enabled_at = (watch_dt - timedelta(seconds=60)).isoformat()
        except Exception:
            pass
        db.execute("""INSERT INTO auto_delete_overrides (scope, scope_id, enabled, enabled_at)
                      VALUES ('movie', ?, 1, ?)
                      ON CONFLICT(scope, scope_id) DO UPDATE SET enabled=1, enabled_at=excluded.enabled_at""",
                   (movie_id, enabled_at))
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
    except Exception as e:
        app.logger.warning(f"Manual sweep failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


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
    m = {}
    try:
        m = jellyfin_get_item(rating_key, {"fields": "ProviderIds,ParentId"})
        library_id = str(m.get("ParentId") or "")
        raw_type = m.get("Type", "")
        item_type = "episode" if raw_type == "Episode" else ("movie" if raw_type == "Movie" else raw_type.lower() or "unknown")
        item_title = m.get("Name", rating_key)
        providers = parse_jellyfin_providers(m.get("ProviderIds") or {})
        if item_type == "episode":
            series_id = str(m.get("SeriesId") or "")
    except Exception as e:
        out.update({"verdict": "JELLYFIN_ERROR",
                    "verdict_detail": f"Could not fetch metadata from Jellyfin: {e}"})
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
        all_account_ids = {a["id"] for a in jellyfin_users()}
        item_assignments, provider_assignments = _load_assignment_maps(db)
        lookup_id = series_id if (item_type == "episode" and series_id) else rating_key
        show_providers = providers if item_type == "movie" else {}
        if item_type == "episode" and series_id:
            try:
                s_data = jellyfin_get_item(series_id, {"fields": "ProviderIds"})
                show_providers = parse_jellyfin_providers(s_data.get("ProviderIds") or {})
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
            else:
                all_watched = False
            # Get timestamp for this user
            user_ts = None
            if providers:
                for ptype, pid in providers.items():
                    row = db.execute(
                        "SELECT updated_at FROM watch_events WHERE account_id=? AND provider_type=? AND provider_id=? AND item_type=? AND event_type='play'",
                        (aid, ptype.lower(), str(pid), item_type)).fetchone()
                    if row and (user_ts is None or row["updated_at"] > user_ts):
                        user_ts = row["updated_at"]
            if user_ts is None:
                row = db.execute(
                    "SELECT updated_at FROM watch_events WHERE account_id=? AND rating_key=? AND event_type='play'",
                    (aid, rating_key)).fetchone()
                if row:
                    user_ts = row["updated_at"]
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
                        "verdict_detail": f"All users have watched. Waiting for minimum post-completion delay ({min_delay_minutes} min, ~{remaining_m} min remaining). The sweep will delete this shortly."})
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
        jellyfin_delete(item_id)
        app.logger.info(f"Deleted item {item_id} from {request.remote_addr}")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/delete-batch", methods=["DELETE"])
@limiter.limit("10 per minute")
@login_required_api
def api_delete_batch():
    ids = request.json.get("itemIds", [])
    ids = [str(i) for i in ids if _ID_RE.match(str(i))]
    deleted, failed = [], []
    for iid in ids:
        try:
            jellyfin_delete(iid)
            deleted.append(iid)
            app.logger.info(f"Deleted batch item {iid} from {request.remote_addr}")
        except Exception as e:
            failed.append({"id": iid, "error": str(e)})
    return jsonify({"success": True, "deleted": deleted, "failed": failed})

@app.route("/api/check-season-empty/<series_id>/<season_id>")
@login_required_api
def api_check_season_empty(series_id, season_id):
    series_id = vid(series_id); season_id = vid(season_id)
    try:
        episodes = jellyfin_items({
            "ParentId": season_id, "IncludeItemTypes": "Episode", "Limit": 1
        })
        return jsonify({"empty": len(episodes) == 0})
    except Exception:
        return jsonify({"empty": False})

@app.route("/api/image/<item_id>")
@limiter.limit("600 per minute")
@login_required_api
def proxy_image(item_id):
    item_id = vid(item_id)
    try:
        w_int = min(max(50, int(float(request.args.get("maxWidth", "300")))), 1000)
    except (ValueError, TypeError):
        w_int = 300
    img_type = request.args.get("type", "Primary")
    if img_type not in ("Primary", "Backdrop", "Thumb", "Banner", "Logo"):
        img_type = "Primary"
    try:
        url = f"{get_jellyfin_url()}/Items/{item_id}/Images/{img_type}"
        r = http_requests.get(url, headers=_jf_headers(),
                               params={"maxWidth": str(w_int), "quality": "90"}, timeout=15, stream=True)
        if r.status_code == 404:
            # Fallback to Primary if requested type missing
            url = f"{get_jellyfin_url()}/Items/{item_id}/Images/Primary"
            r = http_requests.get(url, headers=_jf_headers(),
                                   params={"maxWidth": str(w_int), "quality": "90"}, timeout=15, stream=True)
        r.raise_for_status()
        return Response(r.iter_content(8192), content_type=r.headers.get("Content-Type", "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        return Response(status=404)

if __name__ == "__main__":
    if os.path.exists(CONFIG_PATH):
        os.chmod(CONFIG_PATH, 0o600)
    print(f"LastFrame (Jellyfin)\n  Port: {PORT}\n")
    if is_setup_needed():
        print("  *** First-time setup required ***")
        print(f"  Open http://<your-host>:{PORT} in your browser to complete setup.\n")
    else:
        print(f"  Jellyfin: {get_jellyfin_url()}")
        print(f"  Webhook URL: http://<your-host>:{PORT}/api/webhook/jellyfin")
        ws = get_webhook_secret()
        if ws:
            print("  Webhook secret: configured")
        else:
            print("  Webhook secret: not set (set JELLYFIN_WEBHOOK_SECRET env var to protect the endpoint)")
        print("  Configure in Jellyfin: Dashboard → Webhooks plugin → Add webhook pointing to the URL above")
    print()
    app.run(host="0.0.0.0", port=PORT, debug=False)
