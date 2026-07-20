#!/usr/bin/env python3
"""LastFrame — multi-provider watch-history dashboard (Plex + Jellyfin).

A single instance can talk to a Plex server, a Jellyfin server, or both at once;
each is independently toggleable. List endpoints aggregate across every enabled
provider and item-scoped endpoints dispatch to the engine that owns the item's
namespaced id (see idutil / providers).
"""
import os, sys, secrets, logging, re, hmac, threading, time
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request, session, abort, redirect, url_for, g
import bcrypt
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, CSRFError

from config_store import (
    DATA_DIR, CONFIG_PATH, PAGE_SIZE,
    load_config, save_config, get_admin, verify_admin, update_admin_credentials,
)
from db import get_db, init_db
import idutil
import providers
from plex_api import set_request_hook as _plex_set_request_hook
from jellyfin_api import set_request_hook as _jf_set_request_hook
from assignments import (
    set_assignment_by_provider, delete_assignment_by_provider,
    set_assignment_by_item_id, delete_assignment_by_item_id, get_assigned_ids,
)
from auto_delete import _run_sweep, _auto_delete_sweep
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
_proxy_hops = int(os.environ.get("TRUSTED_PROXY_HOPS", "0") or "0")
if _proxy_hops > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_proxy_hops, x_proto=_proxy_hops,
                            x_host=_proxy_hops, x_prefix=_proxy_hops)

app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
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
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


limiter = Limiter(get_remote_address, app=app,
                  default_limits=["2000 per day", "500 per hour"], storage_uri="memory://")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(os.path.join(DATA_DIR, 'audit.log'))],
)

PORT = int(os.environ.get("PORT", "8080"))

# DB init + background sweeps at import time (works under gunicorn too).
init_db()
_auto_delete_sweep()

# Recently-added discovery sweep (multi-provider).
RECENTLY_ADDED_SWEEP_INTERVAL = int(os.environ.get("RECENTLY_ADDED_SWEEP_INTERVAL", "1800"))


def _recently_added_sweep():
    try:
        if providers.enabled_engines():
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

# Plex-only: poll Plex history so managed (Plex Home) users show up without a
# webhook. Only ticks when Plex is enabled. Set PLEX_HISTORY_POLL_INTERVAL=0 off.
HISTORY_POLL_INTERVAL = int(os.environ.get("PLEX_HISTORY_POLL_INTERVAL", "300"))
HISTORY_POLL_PAGES = int(os.environ.get("PLEX_HISTORY_POLL_PAGES", "1"))


def _history_poll_sweep():
    try:
        plex = providers.get_engine("plex")
        if plex and plex.is_enabled():
            result = plex.import_history(max_pages=HISTORY_POLL_PAGES)
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
    _hp_t = threading.Timer(HISTORY_POLL_INTERVAL, _history_poll_sweep)
    _hp_t.daemon = True
    _hp_t.start()

# Plex-only managed-user viewCount sweep (opt-in; see plex_engine.run_managed_sweep).
MANAGED_USER_SWEEP_INTERVAL = int(os.environ.get("PLEX_MANAGED_USER_SWEEP_INTERVAL", "0"))


def _managed_user_sweep():
    try:
        plex = providers.get_engine("plex")
        if plex and plex.is_enabled():
            report = plex.run_managed_sweep()
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


if MANAGED_USER_SWEEP_INTERVAL > 0:
    _mu_t = threading.Timer(60, _managed_user_sweep)
    _mu_t.daemon = True
    _mu_t.start()


@app.errorhandler(Exception)
def _log_unhandled(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    import traceback
    print(f"[500] {request.method} {request.path}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    traceback.print_exc(file=sys.stderr)
    return jsonify({"error": "Internal server error"}), 500


# ── Perf diagnostics ──────────────────────────────────────────────────
_PERF_PATHS = {
    "/api/users", "/api/libraries", "/api/recent/movies", "/api/recent/episodes",
    "/api/activity", "/api/events/last-update", "/api/watch-summary/items",
}


def _perf_record(path, dur):
    try:
        calls = getattr(g, "_prov_calls", None)
    except RuntimeError:
        return
    if calls is not None:
        calls.append((path, dur))


_plex_set_request_hook(_perf_record)
_jf_set_request_hook(_perf_record)


@app.before_request
def _perf_start():
    if request.path in _PERF_PATHS:
        g._prov_calls = []
        g._req_t0 = time.monotonic()


@app.after_request
def _perf_log(resp):
    t0 = getattr(g, "_req_t0", None)
    if t0 is None:
        return resp
    calls = getattr(g, "_prov_calls", []) or []
    total_ms = int((time.monotonic() - t0) * 1000)
    prov_total_ms = int(sum(d for _, d in calls) * 1000)
    agg = {}
    for p, d in calls:
        slot = agg.setdefault(p, [0, 0.0])
        slot[0] += 1
        slot[1] += d
    parts = sorted(agg.items(), key=lambda x: -x[1][1])[:5]
    breakdown = ", ".join(f"{p}({n}x={int(t * 1000)}ms)" for p, (n, t) in parts) or "-"
    print(f"[PERF] {request.path} {resp.status_code} total={total_ms}ms "
          f"provider_calls={len(calls)} provider_total={prov_total_ms}ms top={breakdown}",
          file=sys.stderr, flush=True)
    return resp


# ── Auth helpers ──────────────────────────────────────────────────────
def is_admin_logged_in():
    return session.get("logged_in") is True


def login_required_api(f):
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_admin_logged_in():
            return jsonify({"error": "Unauthorized", "login_required": True}), 401
        return f(*args, **kwargs)
    return decorated


_USERNAME_RE = re.compile(r'^[a-zA-Z0-9_\-\.@]+$')


def is_setup_needed():
    admin = get_admin()
    if not (admin and admin.get("username") and admin.get("password_hash")):
        return True
    if not providers.configured_engines():
        return True
    return False


# ── Dispatch helpers ──────────────────────────────────────────────────
def _resolve(nsid):
    """(engine, raw) for a namespaced item id, or abort 404/400."""
    e, raw = providers.engine_for_id(nsid)
    if e is None:
        abort(404)
    if not e.valid_raw(raw):
        abort(400)
    return e, raw


def _group_ids(nsids):
    """Group namespaced ids by owning enabled engine: {key: (engine, [raw, ...])}."""
    groups = {}
    cfg = load_config()
    for nsid in nsids:
        e, raw = providers.engine_for_id(nsid, cfg)
        if e is None or not e.valid_raw(raw):
            continue
        groups.setdefault(e.KEY, (e, []))[1].append(raw)
    return groups


def _label_accounts(engine_lists, multi):
    """Flatten per-engine account lists, tagging names with the provider when
    more than one provider is active so identically-named users stay distinct."""
    out = []
    for engine, accs in engine_lists:
        for a in accs:
            item = dict(a)
            if multi:
                item["name"] = f"{a['name']} ({engine.LABEL})"
            out.append(item)
    return out


# ── Routes: pages / auth ──────────────────────────────────────────────
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
        resp["jellyfin_url_from_env"] = os.environ.get("JELLYFIN_URL", "")
        resp["jellyfin_api_key_set"] = bool(os.environ.get("JELLYFIN_API_KEY"))
        resp["webhook_secret_set"] = bool(os.environ.get("PLEX_WEBHOOK_SECRET") or os.environ.get("JELLYFIN_WEBHOOK_SECRET"))
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

    plex_ok = bool(plex_token or os.environ.get("PLEX_TOKEN"))
    jf_ok = bool(jellyfin_api_key or os.environ.get("JELLYFIN_API_KEY"))
    if not (plex_ok or jf_ok):
        return jsonify({"success": False, "error": "Configure at least one provider (Plex token or Jellyfin API key)"}), 400

    env_secret = bool(os.environ.get("PLEX_WEBHOOK_SECRET") or os.environ.get("JELLYFIN_WEBHOOK_SECRET"))
    if not env_secret and not webhook_secret:
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
    if jellyfin_url:
        cfg["jellyfin_url"] = jellyfin_url
    if jellyfin_api_key:
        cfg["jellyfin_api_key"] = jellyfin_api_key
    if webhook_secret:
        cfg["webhook_secret"] = webhook_secret
    cfg["plex_enabled"] = plex_ok
    cfg["jellyfin_enabled"] = jf_ok
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
    session.pop("logged_in", None)
    session.pop("username", None)
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
        session.pop("logged_in", None)
        session.pop("username", None)
        return jsonify({"success": True, "message": msg})
    return jsonify({"success": False, "error": msg}), 400


# ── Routes: config ────────────────────────────────────────────────────
@app.route("/api/config")
@login_required_api
def api_get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
@login_required_api
def api_save_config():
    cfg = load_config()
    incoming = request.json or {}
    cfg["monitored_libraries"] = incoming.get("monitored_libraries", cfg.get("monitored_libraries", []))
    cfg["show_all_libraries"] = incoming.get("show_all_libraries", cfg.get("show_all_libraries", True))
    # Provider master switches and (optional) credential updates.
    for flag in ("plex_enabled", "jellyfin_enabled"):
        if flag in incoming:
            cfg[flag] = bool(incoming[flag])
    for field in ("plex_url", "plex_token", "jellyfin_url", "jellyfin_api_key", "webhook_secret"):
        if field in incoming and (incoming[field] or "").strip():
            cfg[field] = incoming[field].strip()
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


@app.route("/api/providers")
@login_required_api
def api_providers():
    """Provider status for the settings UI: configured / enabled per provider."""
    cfg = load_config()
    return jsonify([{"key": e.KEY, "label": e.LABEL, "configured": e.is_configured(),
                     "enabled": e.is_enabled(cfg), "webhookPath": e.WEBHOOK_PATH}
                    for e in providers.all_engines()])


# ── Routes: webhooks ──────────────────────────────────────────────────
def _handle_webhook(engine):
    secret = engine.webhook_secret()
    if not secret:
        return jsonify({"error": "Webhook not configured"}), 503
    incoming = request.headers.get("X-Webhook-Secret") or request.args.get("secret", "")
    if not hmac.compare_digest(incoming, secret):
        return jsonify({"error": "Unauthorized"}), 401
    body, status = engine.store_webhook(request, load_config())
    return jsonify(body), status


@app.route("/api/webhook/plex", methods=["POST"])
@csrf.exempt
@limiter.limit("120 per minute")
def api_webhook_plex():
    return _handle_webhook(providers.get_engine("plex"))


@app.route("/api/webhook/jellyfin", methods=["POST"])
@csrf.exempt
@limiter.limit("120 per minute")
def api_webhook_jellyfin():
    return _handle_webhook(providers.get_engine("jellyfin"))


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
        "accounts": [{"account_id": r["account_id"], "play_events": r["cnt"]} for r in accounts],
    })


def _account_names():
    names = {}
    for e in providers.enabled_engines():
        try:
            for a in e.accounts():
                names[a["id"]] = a["name"]
        except Exception:
            continue
    return names


@app.route("/api/webhook-events/for-item/<rating_key>")
@login_required_api
def api_webhook_events_for_item(rating_key):
    _resolve(rating_key)  # validate ownership/format
    db = get_db()
    rows = db.execute(
        "SELECT account_id, provider_type, provider_id, item_type, event_type, updated_at, rating_key "
        "FROM watch_events WHERE rating_key=? ORDER BY updated_at DESC", (rating_key,)).fetchall()
    recent = db.execute(
        "SELECT account_id, item_type, rating_key, updated_at FROM watch_events "
        "ORDER BY updated_at DESC LIMIT 20").fetchall()
    db.close()
    name_by_id = _account_names()
    return jsonify({
        "rating_key": rating_key,
        "events_for_item": [
            {"account_id": r["account_id"], "account_name": name_by_id.get(r["account_id"], "?"),
             "provider_type": r["provider_type"], "provider_id": r["provider_id"],
             "item_type": r["item_type"], "event_type": r["event_type"],
             "updated_at": r["updated_at"], "rating_key": r["rating_key"]} for r in rows],
        "most_recent_overall": [
            {"account_id": r["account_id"], "account_name": name_by_id.get(r["account_id"], "?"),
             "item_type": r["item_type"], "rating_key": r["rating_key"], "updated_at": r["updated_at"]} for r in recent],
    })


@app.route("/api/webhook-events", methods=["DELETE"])
@login_required_api
def api_webhook_events_clear():
    db = get_db()
    db.execute("DELETE FROM watch_events")
    db.commit()
    db.close()
    return jsonify({"success": True})


# ── Routes: item info / browse ────────────────────────────────────────
@app.route("/api/item/<item_id>")
@login_required_api
def api_item_info(item_id):
    engine, raw = _resolve(item_id)
    info = engine.item_info(raw)
    if not info:
        return jsonify({"error": "not found"}), 404
    return jsonify(info)


@app.route("/api/users")
@login_required_api
def api_users():
    engines = providers.enabled_engines()
    multi = len(engines) > 1
    return jsonify(_label_accounts([(e, e.accounts()) for e in engines], multi))


@app.route("/api/libraries")
@login_required_api
def api_libraries():
    cfg = load_config()
    out = []
    for e in providers.enabled_engines(cfg):
        try:
            out.extend(e.libraries(cfg))
        except Exception:
            app.logger.warning(f"libraries() failed for {e.KEY}")
    return jsonify(out)


def _list_titles(kind):
    pid = request.args.get("parentId", "")
    search = request.args.get("search", "")
    genre = request.args.get("genre", "").strip()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    ids = request.args.get("ids", "")

    if ids:
        groups = _group_ids([i.strip() for i in ids.split(",") if i.strip()])
        items = []
        for engine, raws in groups.values():
            items.extend(engine.titles_by_ids(kind, raws))
        return jsonify({"items": items, "totalCount": len(items), "page": 1,
                        "pageSize": len(items), "isSearch": False})

    if not pid:
        return jsonify({"items": [], "totalCount": 0, "page": page, "pageSize": PAGE_SIZE, "isSearch": False})

    engine, raw = _resolve(pid)
    data = engine.list_titles(kind, raw, page, search, genre)
    return jsonify({"items": data["items"], "totalCount": data["totalCount"], "page": page,
                    "pageSize": PAGE_SIZE, "isSearch": bool(search)})


@app.route("/api/series")
@login_required_api
def api_series():
    return _list_titles("series")


@app.route("/api/movies")
@login_required_api
def api_movies():
    return _list_titles("movies")


@app.route("/api/search")
@login_required_api
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"items": [], "query": ""})
    cfg = load_config()
    cfg = dict(cfg)
    cfg["_q"] = q
    results = []
    for e in providers.enabled_engines(cfg):
        try:
            results.extend(e.search(cfg))
        except Exception:
            continue
    qlow = q.lower()
    results.sort(key=lambda x: (0 if (x["name"] or "").lower().startswith(qlow) else 1, (x["name"] or "").lower()))
    return jsonify({"items": results, "query": q})


@app.route("/api/watch-summary")
@login_required_api
def api_watch_summary():
    pid = request.args.get("parentId", "")
    kind = "movies" if request.args.get("type", "movies") == "movies" else "series"
    if not pid:
        return jsonify({})
    engine, raw = _resolve(pid)
    return jsonify(engine.watch_summary(raw, kind))


@app.route("/api/genres")
@login_required_api
def api_genres():
    pid = request.args.get("parentId", "")
    if not pid:
        return jsonify([])
    engine, raw = _resolve(pid)
    try:
        return jsonify(engine.genres(raw))
    except Exception:
        app.logger.exception("Failed to fetch genres")
        return jsonify({"error": "Internal server error"}), 500


@app.route("/api/seasons/<series_id>")
@login_required_api
def api_seasons(series_id):
    engine, raw = _resolve(series_id)
    return jsonify(engine.seasons(raw))


@app.route("/api/season-watch-status/<series_id>/<season_id>")
@login_required_api
def api_season_watch_status(series_id, season_id):
    engine, series_raw = _resolve(series_id)
    engine2, season_raw = _resolve(season_id)
    if engine2.KEY != engine.KEY:
        abort(400)
    return jsonify(engine.season_watch_status(series_raw, season_raw))


@app.route("/api/watch-status/<item_id>")
@login_required_api
def api_watch_status(item_id):
    engine, raw = _resolve(item_id)
    return jsonify(engine.watch_status(raw))


@app.route("/api/watch-summary/items")
@login_required_api
def api_watch_summary_items():
    raw_movie_ids = [x for x in (request.args.get("movieIds", "") or "").split(",") if x]
    raw_ep_ids = [x for x in (request.args.get("episodeIds", "") or "").split(",") if x]
    if not raw_movie_ids and not raw_ep_ids:
        return jsonify({})
    movie_groups = _group_ids(raw_movie_ids)
    ep_groups = _group_ids(raw_ep_ids)
    result = {}
    for key in set(movie_groups) | set(ep_groups):
        engine = (movie_groups.get(key) or ep_groups.get(key))[0]
        m_raws = movie_groups.get(key, (None, []))[1]
        e_raws = ep_groups.get(key, (None, []))[1]
        try:
            result.update(engine.watch_summary_items(m_raws, e_raws))
        except Exception:
            app.logger.warning(f"watch_summary_items failed for {key}")
    return jsonify(result)


# ── Routes: recent / activity ─────────────────────────────────────────
def _recent(kind):
    cfg = load_config()
    merged = []
    for e in providers.enabled_engines(cfg):
        try:
            merged.extend(e.recent(kind, cfg))
        except Exception:
            app.logger.warning(f"recent({kind}) failed for {e.KEY}")
    merged.sort(key=lambda x: x.get("lastPlayedDate") or "", reverse=True)
    page = request.args.get("page", type=int)
    if page is not None:
        page = max(1, page)
        offset = (page - 1) * PAGE_SIZE
        return jsonify({"items": merged[offset:offset + PAGE_SIZE], "totalCount": len(merged),
                        "page": page, "pageSize": PAGE_SIZE, "isSearch": False})
    limit = request.args.get("limit", 10, type=int)
    return jsonify(merged[:limit])


@app.route("/api/recent/movies")
@login_required_api
def api_recent_movies():
    return _recent("movies")


@app.route("/api/recent/episodes")
@login_required_api
def api_recent_episodes():
    return _recent("episodes")


@app.route("/api/activity")
@login_required_api
def api_activity():
    out = []
    for e in providers.enabled_engines():
        try:
            out.extend(e.activity())
        except Exception:
            continue
    return jsonify(out)


@app.route("/api/events/last-update")
@login_required_api
def api_events_last_update():
    db = get_db()
    row = db.execute("SELECT MAX(updated_at) as last_at FROM watch_events").fetchone()
    db.close()
    return jsonify({"last_at": row["last_at"] if row else None})


@app.route("/api/admin/backfill-history", methods=["POST"])
@limiter.limit("2 per hour")
@login_required_api
def api_backfill_history():
    engines = providers.enabled_engines()
    if not engines:
        return jsonify({"ok": False, "error": "no enabled providers"}), 400
    imported = 0
    total = 0
    per = {}
    for e in engines:
        try:
            r = e.backfill()
            imported += r.get("imported", 0)
            total += r.get("total_history", 0)
            per[e.KEY] = r
        except Exception as ex:
            per[e.KEY] = {"ok": False, "error": str(ex)}
    return jsonify({"ok": True, "imported": imported, "total_history": total, "providers": per})


# ── Routes: accounts ──────────────────────────────────────────────────
@app.route("/api/accounts")
@login_required_api
def api_accounts_all():
    out = []
    for e in providers.enabled_engines():
        try:
            out.extend(e.all_accounts())
        except Exception:
            continue
    return jsonify(out)


def _valid_account_id(account_id):
    key, raw = idutil.split_id(account_id)
    return key is not None and bool(raw)


@app.route("/api/accounts/<account_id>/hide", methods=["POST"])
@login_required_api
def api_hide_account(account_id):
    if not _valid_account_id(account_id):
        abort(400)
    cfg = load_config()
    hidden = set(cfg.get("hidden_accounts", []))
    hidden.add(account_id)
    cfg["hidden_accounts"] = list(hidden)
    save_config(cfg)
    return jsonify({"success": True})


@app.route("/api/accounts/<account_id>/hide", methods=["DELETE"])
@login_required_api
def api_unhide_account(account_id):
    if not _valid_account_id(account_id):
        abort(400)
    cfg = load_config()
    hidden = set(cfg.get("hidden_accounts", []))
    hidden.discard(account_id)
    cfg["hidden_accounts"] = list(hidden)
    save_config(cfg)
    return jsonify({"success": True})


# ── Routes: assignments ───────────────────────────────────────────────
@app.route("/api/assignments/<item_id>")
@login_required_api
def api_get_assignments(item_id):
    _resolve(item_id)
    assigned = get_assigned_ids(item_id)
    if assigned is None:
        return jsonify({"assigned": [], "mode": "all"})
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
    _resolve(item_id)
    user_ids = request.json.get("userIds", [])
    user_ids = [str(u) for u in user_ids if _valid_account_id(str(u))]
    if not set_assignment_by_provider(item_id, user_ids):
        set_assignment_by_item_id(item_id, user_ids)
    recently_added.mark_handled(item_id)
    return jsonify({"success": True})


@app.route("/api/assignments/<item_id>", methods=["DELETE"])
@login_required_api
def api_delete_assignments(item_id):
    _resolve(item_id)
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
    # Keep only ids owned by an enabled provider.
    cfg = load_config()
    item_ids = [str(i) for i in item_ids if providers.engine_for_id(str(i), cfg)[0] is not None]
    user_ids = [str(u) for u in user_ids if _valid_account_id(str(u))]
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


# ── Routes: auto-delete ───────────────────────────────────────────────
@app.route("/api/auto-delete/series/<series_id>", methods=["GET"])
@login_required_api
def api_auto_delete_series_get(series_id):
    engine, raw = _resolve(series_id)
    return jsonify(engine.auto_delete_status("series", raw, load_config()))


@app.route("/api/auto-delete/movie/<movie_id>", methods=["GET"])
@login_required_api
def api_auto_delete_movie_get(movie_id):
    engine, raw = _resolve(movie_id)
    return jsonify(engine.auto_delete_status("movie", raw, load_config()))


def _set_override(scope, engine, raw, nsid, enabled):
    global_enabled_was_off = False
    db = get_db()
    if enabled is None:
        db.execute("DELETE FROM auto_delete_overrides WHERE scope=? AND scope_id=?", (scope, nsid))
    elif enabled:
        enabled_at = engine.enabled_at_for_override(raw, db)
        db.execute(
            "INSERT INTO auto_delete_overrides (scope, scope_id, enabled, enabled_at) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(scope, scope_id) DO UPDATE SET enabled=1, enabled_at=excluded.enabled_at",
            (scope, nsid, enabled_at))
        cfg = load_config()
        if not cfg.get("auto_delete_enabled"):
            cfg["auto_delete_enabled"] = True
            save_config(cfg)
            global_enabled_was_off = True
    else:
        db.execute(
            "INSERT INTO auto_delete_overrides (scope, scope_id, enabled) VALUES (?, ?, 0) "
            "ON CONFLICT(scope, scope_id) DO UPDATE SET enabled=0", (scope, nsid))
    db.commit()
    db.close()
    return global_enabled_was_off


@app.route("/api/auto-delete/series/<series_id>", methods=["POST"])
@login_required_api
def api_auto_delete_series_set(series_id):
    engine, raw = _resolve(series_id)
    enabled = (request.json or {}).get("enabled")
    was_off = _set_override("series", engine, raw, series_id, enabled)
    return jsonify({"success": True, "global_enabled_was_off": was_off})


@app.route("/api/auto-delete/movie/<movie_id>", methods=["POST"])
@login_required_api
def api_auto_delete_movie_set(movie_id):
    engine, raw = _resolve(movie_id)
    enabled = (request.json or {}).get("enabled")
    was_off = _set_override("movie", engine, raw, movie_id, enabled)
    return jsonify({"success": True, "global_enabled_was_off": was_off})


@app.route("/api/auto-delete/sweep", methods=["POST"])
@login_required_api
def api_auto_delete_sweep():
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
    engine, raw = _resolve(rating_key)
    return jsonify(engine.diagnose(raw, load_config()))


# ── Routes: delete / image / misc ─────────────────────────────────────
@app.route("/api/delete/<item_id>", methods=["DELETE"])
@limiter.limit("30 per minute")
@login_required_api
def api_delete_item(item_id):
    engine, raw = _resolve(item_id)
    try:
        engine.delete(raw)
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
    groups = _group_ids([str(i) for i in ids])
    deleted, failed = [], []
    for engine, raws in groups.values():
        for raw in raws:
            nsid = idutil.make_id(engine.KEY, raw)
            try:
                engine.delete(raw)
                deleted.append(nsid)
                app.logger.info(f"Deleted batch item {nsid} from {request.remote_addr}")
            except Exception:
                app.logger.exception(f"Failed to delete batch item {nsid} from {request.remote_addr}")
                failed.append({"id": nsid, "error": "Failed to delete item"})
    return jsonify({"success": True, "deleted": deleted, "failed": failed})


@app.route("/api/check-season-empty/<series_id>/<season_id>")
@login_required_api
def api_check_season_empty(series_id, season_id):
    engine, season_raw = _resolve(season_id)
    return jsonify({"empty": engine.check_season_empty(season_raw)})


@app.route("/api/image/<item_id>")
@limiter.limit("600 per minute")
@login_required_api
def proxy_image(item_id):
    engine, raw = _resolve(item_id)
    try:
        w_int = min(max(50, int(float(request.args.get("maxWidth", "300")))), 1000)
    except (ValueError, TypeError):
        w_int = 300
    img_type = request.args.get("type", "Primary")
    return engine.image_response(raw, w_int, img_type)


# ── Routes: Plex-only diagnostics ─────────────────────────────────────
@app.route("/api/admin/debug-history")
@login_required_api
def api_debug_history():
    plex = providers.get_engine("plex")
    if not plex or not plex.is_enabled():
        return jsonify({"ok": False, "error": "Plex is not enabled"}), 400
    key = (request.args.get("key") or "").strip()
    title_q = (request.args.get("title") or "").strip().lower()
    body, status = plex.debug_history(key, title_q)
    return jsonify(body), status


@app.route("/api/admin/debug-managed-sweep", methods=["GET", "POST"])
@login_required_api
def api_debug_managed_sweep():
    plex = providers.get_engine("plex")
    if not plex or not plex.is_enabled():
        return jsonify({"ok": False, "error": "Plex is not enabled"}), 400
    return jsonify(plex.run_managed_sweep())


if __name__ == "__main__":
    if os.path.exists(CONFIG_PATH):
        os.chmod(CONFIG_PATH, 0o600)
    print(f"LastFrame (Plex + Jellyfin)\n  Port: {PORT}\n")
    if is_setup_needed():
        print("  *** First-time setup required ***")
        print(f"  Open http://<your-host>:{PORT} in your browser to complete setup.\n")
    else:
        cfg = load_config()
        for e in providers.all_engines():
            state = "enabled" if e.is_enabled(cfg) else ("configured (off)" if e.is_configured() else "not configured")
            print(f"  {e.LABEL}: {state}   webhook → http://<your-host>:{PORT}{e.WEBHOOK_PATH}")
    print()
    app.run(host="0.0.0.0", port=PORT, debug=False)
