#!/usr/bin/env python3
"""Jellyfin Watch History Dashboard with Authentication & Security"""

import json, os, sys, sqlite3, secrets, logging
from flask import Flask, render_template, jsonify, request, Response, session
import requests as http_requests
import bcrypt
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)

# Fix IP detection behind reverse proxy
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Session security
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax'
)

# Rate limiting
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["2000 per day", "500 per hour"],
    storage_uri="memory://",
)

# Audit logging
logging.basicConfig(
    filename=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audit.log'),
    level=logging.INFO,
    format='%(asctime)s - %(message)s'
)

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://127.0.0.1:8096")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
PORT = int(os.environ.get("PORT", "8080"))
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
DB_PATH = os.path.join(APP_DIR, "data.db")
PAGE_SIZE = 50
PROVIDER_PRIORITY = ["Tmdb", "Imdb", "Tvdb"]

# ── Config helpers ────────────────────────────────────────────────────
def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f: return json.load(f)
    return {"monitored_libraries": [], "show_all_libraries": True}

def save_config(config):
    with open(CONFIG_PATH, "w") as f: json.dump(config, f, indent=2)

def get_admin():
    cfg = load_config()
    return cfg.get("admin", {})

def set_admin(username, password_hash):
    cfg = load_config()
    cfg["admin"] = {"username": username, "password_hash": password_hash}
    save_config(cfg)

def verify_admin(username, password):
    admin = get_admin()
    if not admin:
        return False
    if username != admin.get("username"):
        return False
    pwd_hash = admin.get("password_hash")
    if not pwd_hash:
        return False
    return bcrypt.checkpw(password.encode('utf-8'), pwd_hash.encode('utf-8'))

def update_admin_credentials(current_username, current_password, new_username, new_password):
    if not verify_admin(current_username, current_password):
        return False, "Current credentials are incorrect"
    if not new_username or not new_password:
        return False, "Username and password cannot be empty"
    salt = bcrypt.gensalt()
    new_hash = bcrypt.hashpw(new_password.encode('utf-8'), salt).decode('utf-8')
    set_admin(new_username, new_hash)
    return True, "Credentials updated"

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

# ── Database ──────────────────────────────────────────────────────────
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS assignments (
        item_id TEXT NOT NULL, user_id TEXT NOT NULL,
        PRIMARY KEY (item_id, user_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS assignments_by_provider (
        provider_type TEXT NOT NULL, provider_id TEXT NOT NULL, user_id TEXT NOT NULL,
        PRIMARY KEY (provider_type, provider_id, user_id))""")
    db.commit(); db.close()

# ── Provider ID helpers ───────────────────────────────────────────────
def get_item_providers(item_id):
    try:
        item = jf_get(f"/Items/{item_id}")
        return item.get("ProviderIds", {})
    except:
        return {}

def get_assignment_by_provider(item_id):
    providers = get_item_providers(item_id)
    if not providers:
        return None
    db = get_db()
    for ptype in PROVIDER_PRIORITY:
        pid = providers.get(ptype)
        if pid:
            rows = db.execute(
                "SELECT user_id FROM assignments_by_provider WHERE provider_type = ? AND provider_id = ?",
                (ptype, str(pid))
            ).fetchall()
            if rows:
                db.close()
                return set(r[0] for r in rows)
    db.close()
    return None

def set_assignment_by_provider(item_id, user_ids):
    providers = get_item_providers(item_id)
    if not providers:
        return False
    db = get_db()
    for ptype in PROVIDER_PRIORITY:
        pid = providers.get(ptype)
        if pid:
            db.execute("DELETE FROM assignments_by_provider WHERE provider_type = ? AND provider_id = ?",
                       (ptype, str(pid)))
            for uid in user_ids:
                db.execute("INSERT INTO assignments_by_provider (provider_type, provider_id, user_id) VALUES (?, ?, ?)",
                           (ptype, str(pid), uid))
            db.commit()
            db.close()
            return True
    db.close()
    return False

def delete_assignment_by_provider(item_id):
    providers = get_item_providers(item_id)
    if not providers:
        return False
    db = get_db()
    for ptype in PROVIDER_PRIORITY:
        pid = providers.get(ptype)
        if pid:
            db.execute("DELETE FROM assignments_by_provider WHERE provider_type = ? AND provider_id = ?",
                       (ptype, str(pid)))
            db.commit()
            db.close()
            return True
    db.close()
    return False

def get_assignment_by_item_id(item_id):
    db = get_db()
    rows = db.execute("SELECT user_id FROM assignments WHERE item_id = ?", (item_id,)).fetchall()
    db.close()
    return set(r[0] for r in rows) if rows else None

def set_assignment_by_item_id(item_id, user_ids):
    db = get_db()
    db.execute("DELETE FROM assignments WHERE item_id = ?", (item_id,))
    for uid in user_ids:
        db.execute("INSERT INTO assignments (item_id, user_id) VALUES (?, ?)", (item_id, uid))
    db.commit()
    db.close()

def delete_assignment_by_item_id(item_id):
    db = get_db()
    db.execute("DELETE FROM assignments WHERE item_id = ?", (item_id,))
    db.commit()
    db.close()

def get_assigned_ids(item_id):
    assigned = get_assignment_by_provider(item_id)
    if assigned is not None:
        return assigned
    return get_assignment_by_item_id(item_id)

# ── Jellyfin API ──────────────────────────────────────────────────────
def jf_get(path, params=None):
    r = http_requests.get(f"{JELLYFIN_URL}{path}", headers={"X-Emby-Token": JELLYFIN_API_KEY}, params=params, timeout=15)
    r.raise_for_status(); return r.json()

def jf_delete(path):
    r = http_requests.delete(f"{JELLYFIN_URL}{path}", headers={"X-Emby-Token": JELLYFIN_API_KEY}, timeout=30)
    r.raise_for_status(); return r

def jf_get_raw(path, params=None):
    r = http_requests.get(f"{JELLYFIN_URL}{path}", headers={"X-Emby-Token": JELLYFIN_API_KEY}, params=params, timeout=15, stream=True)
    r.raise_for_status(); return r

# ── Routes ────────────────────────────────────────────────────────────
@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/auth/status")
def auth_status():
    return jsonify({"logged_in": is_admin_logged_in(), "username": session.get("username")})

@app.route("/api/auth/login", methods=["POST"])
@limiter.limit("5 per minute")
def api_login():
    data = request.json
    username = data.get("username", "")
    password = data.get("password", "")
    if verify_admin(username, password):
        session["logged_in"] = True
        session["username"] = username
        app.logger.info(f"Successful login for '{username}' from {request.remote_addr}")
        return jsonify({"success": True})
    app.logger.warning(f"Failed login attempt for '{username}' from {request.remote_addr}")
    return jsonify({"success": False, "error": "Invalid credentials"}), 401

@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.pop("logged_in", None)
    session.pop("username", None)
    return jsonify({"success": True})

@app.route("/api/admin/change-credentials", methods=["POST"])
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
    if new_password != confirm_password:
        return jsonify({"success": False, "error": "New passwords do not match"}), 400
    
    success, msg = update_admin_credentials(current_username, current_password, new_username, new_password)
    if success:
        app.logger.info(f"Admin credentials changed from {current_username} to {new_username} from {request.remote_addr}")
        session.pop("logged_in", None)
        session.pop("username", None)
        return jsonify({"success": True, "message": msg})
    else:
        return jsonify({"success": False, "error": msg}), 400

# ── Protected API endpoints ──
@app.route("/api/config")
@login_required_api
def api_get_config(): return jsonify(load_config())

@app.route("/api/config", methods=["POST"])
@login_required_api
def api_save_config(): save_config(request.json); return jsonify({"success": True})

@app.route("/api/item/<item_id>")
@login_required_api
def api_item_info(item_id):
    users = jf_get("/Users")
    if not users: return jsonify({"error": "no users"}), 500
    item = jf_get(f"/Users/{users[0]['Id']}/Items/{item_id}")
    return jsonify({"id": item["Id"], "name": item.get("Name", ""), "type": item.get("Type", ""),
        "collectionType": item.get("CollectionType", ""), "seriesId": item.get("SeriesId"),
        "seriesName": item.get("SeriesName")})

@app.route("/api/users")
@login_required_api
def api_users(): return jsonify([{"id": u["Id"], "name": u["Name"]} for u in jf_get("/Users")])

@app.route("/api/libraries")
@login_required_api
def api_libraries():
    users = jf_get("/Users")
    if not users: return jsonify([])
    views = jf_get(f"/Users/{users[0]['Id']}/Views")
    config = load_config()
    out = []
    for item in views.get("Items", []):
        ct = item.get("CollectionType", "")
        if ct not in ("tvshows", "movies"): continue
        lid = item["Id"]
        out.append({"id": lid, "name": item["Name"], "type": ct,
            "monitored": config.get("show_all_libraries", True) or lid in config.get("monitored_libraries", [])})
    return jsonify(out)

@app.route("/api/series")
@login_required_api
def api_series():
    pid = request.args.get("parentId", "")
    search = request.args.get("search", "")
    page = int(request.args.get("page", "1"))
    params = {"IncludeItemTypes": "Series", "Recursive": "true", "SortBy": "SortName", "SortOrder": "Ascending",
        "Fields": "BasicSyncInfo", "ImageTypeLimit": 1, "EnableTotalRecordCount": "true"}
    if search:
        params["SearchTerm"] = search
        params["Limit"] = 100
    else:
        params["Limit"] = PAGE_SIZE
        params["StartIndex"] = (page - 1) * PAGE_SIZE
    if pid: params["ParentId"] = pid
    ids = request.args.get("ids", "")
    if ids:
        params["Ids"] = ids
        params.pop("StartIndex", None)
        params["Limit"] = 10000
    genre = request.args.get("genre", "")
    if genre: params["Genres"] = genre
    data = jf_get("/Items", params)
    return jsonify({"items": [{"id": i["Id"], "name": i["Name"], "year": i.get("ProductionYear")} for i in data.get("Items", [])],
        "totalCount": data.get("TotalRecordCount", 0), "page": page, "pageSize": PAGE_SIZE, "isSearch": bool(search)})

@app.route("/api/movies")
@login_required_api
def api_movies():
    pid = request.args.get("parentId", "")
    search = request.args.get("search", "")
    page = int(request.args.get("page", "1"))
    params = {"IncludeItemTypes": "Movie", "Recursive": "true", "SortBy": "SortName", "SortOrder": "Ascending",
        "Fields": "BasicSyncInfo", "ImageTypeLimit": 1, "EnableTotalRecordCount": "true"}
    if search:
        params["SearchTerm"] = search
        params["Limit"] = 100
    else:
        params["Limit"] = PAGE_SIZE
        params["StartIndex"] = (page - 1) * PAGE_SIZE
    if pid: params["ParentId"] = pid
    ids = request.args.get("ids", "")
    if ids:
        params["Ids"] = ids
        params.pop("StartIndex", None)
        params["Limit"] = 10000
    genre = request.args.get("genre", "")
    if genre: params["Genres"] = genre
    data = jf_get("/Items", params)
    return jsonify({"items": [{"id": i["Id"], "name": i["Name"], "year": i.get("ProductionYear")} for i in data.get("Items", [])],
        "totalCount": data.get("TotalRecordCount", 0), "page": page, "pageSize": PAGE_SIZE, "isSearch": bool(search)})

@app.route("/api/watch-summary")
@login_required_api
def api_watch_summary():
    pid = request.args.get("parentId", "")
    item_type = request.args.get("type", "movies")
    include_type = "Movie" if item_type == "movies" else "Series"
    users = jf_get("/Users")
    if not users:
        return jsonify({})
    all_user_ids = {u["Id"] for u in users}
    user_played = {}
    all_item_ids = []
    for u in users:
        params = {"IncludeItemTypes": include_type, "Recursive": "true",
                  "Fields": "BasicSyncInfo", "EnableTotalRecordCount": "false", "Limit": 10000}
        if pid: params["ParentId"] = pid
        try:
            data = jf_get(f"/Users/{u['Id']}/Items", params)
            items = data.get("Items", [])
            if not all_item_ids:
                all_item_ids = [i["Id"] for i in items]
            user_played[u["Id"]] = {i["Id"] for i in items
                                     if i.get("UserData", {}).get("Played", False)}
        except:
            user_played[u["Id"]] = set()
    if not all_item_ids:
        return jsonify({})
    db = get_db()
    rows = db.execute("SELECT item_id, user_id FROM assignments").fetchall()
    db.close()
    item_assignments = {}
    for row in rows:
        item_assignments.setdefault(row["item_id"], set()).add(row["user_id"])
    result = {}
    for iid in all_item_ids:
        target_ids = item_assignments.get(iid, all_user_ids)
        if not target_ids:
            result[iid] = False
            continue
        result[iid] = all(iid in user_played.get(u_id, set()) for u_id in target_ids)
    return jsonify(result)

@app.route("/api/genres")
@login_required_api
def api_genres():
    pid = request.args.get("parentId", "")
    item_type = request.args.get("type", "movies")
    include_type = "Movie" if item_type == "movies" else "Series"
    users = jf_get("/Users")
    if not users:
        return jsonify([])
    uid = users[0]["Id"]
    params = {"IncludeItemTypes": include_type, "Recursive": "true",
              "Fields": "Genres", "ImageTypeLimit": 0,
              "EnableTotalRecordCount": "false", "Limit": 10000}
    if pid: params["ParentId"] = pid
    try:
        data = jf_get(f"/Users/{uid}/Items", params)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    genre_set = set()
    for item in data.get("Items", []):
        for g in item.get("Genres", []):
            genre_set.add(g)
    return jsonify(sorted(genre_set))

@app.route("/api/seasons/<series_id>")
@login_required_api
def api_seasons(series_id):
    users = jf_get("/Users")
    if not users:
        return jsonify([])
    uid = users[0]["Id"]
    data = jf_get(f"/Shows/{series_id}/Seasons", {"userId": uid})
    seasons = [{"id": i["Id"], "name": i["Name"], "indexNumber": i.get("IndexNumber", 0)} for i in data.get("Items", [])]
    if not seasons:
        return jsonify(seasons)
    assigned_ids = get_assigned_ids(series_id)
    target_users = users if assigned_ids is None else [u for u in users if u["Id"] in assigned_ids]
    season_episode_total = {}
    season_user_progress = {}
    for user in target_users:
        params = {"userId": user["Id"], "Fields": "BasicSyncInfo"}
        eps_data = jf_get(f"/Shows/{series_id}/Episodes", params)
        for ep in eps_data.get("Items", []):
            season_id_ep = ep.get("SeasonId")
            if not season_id_ep:
                continue
            if season_id_ep not in season_episode_total:
                season_episode_total[season_id_ep] = 0
            season_episode_total[season_id_ep] += 1
            if season_id_ep not in season_user_progress:
                season_user_progress[season_id_ep] = {}
            if user["Id"] not in season_user_progress[season_id_ep]:
                season_user_progress[season_id_ep][user["Id"]] = 0
            if ep.get("UserData", {}).get("Played", False):
                season_user_progress[season_id_ep][user["Id"]] += 1
    result = []
    for s in seasons:
        sid = s["id"]
        total_eps = season_episode_total.get(sid, 0)
        user_counts = season_user_progress.get(sid, {})
        completed_users = 0
        per_user = []
        for user in target_users:
            played = user_counts.get(user["Id"], 0)
            completed = (played >= total_eps and total_eps > 0)
            if completed:
                completed_users += 1
            per_user.append({
                "userId": user["Id"],
                "userName": user["Name"],
                "playedCount": played,
                "totalCount": total_eps,
                "completed": completed
            })
        result.append({
            "id": s["id"],
            "name": s["name"],
            "indexNumber": s["indexNumber"],
            "totalEpisodes": total_eps,
            "userProgress": per_user,
            "completedUsers": completed_users,
            "totalAssignedUsers": len(target_users)
        })
    return jsonify(result)

@app.route("/api/season-watch-status/<series_id>/<season_id>")
@login_required_api
def api_season_watch_status(series_id, season_id):
    users = jf_get("/Users")
    all_eps, ep_order = {}, []
    for user in users:
        try: data = jf_get(f"/Shows/{series_id}/Episodes", {"SeasonId": season_id, "userId": user["Id"], "Fields": "BasicSyncInfo,Path"})
        except: continue
        for ep in data.get("Items", []):
            eid = ep["Id"]
            if eid not in all_eps:
                all_eps[eid] = {"id": eid, "name": ep.get("Name", ""), "indexNumber": ep.get("IndexNumber", 0), "runTimeTicks": ep.get("RunTimeTicks", 0), "users": []}
                ep_order.append(eid)
            ud = ep.get("UserData", {})
            pct = ud.get("PlayedPercentage", 0) or 0
            if ud.get("Played", False) and pct == 0: pct = 100.0
            all_eps[eid]["users"].append({"userId": user["Id"], "userName": user["Name"], "played": ud.get("Played", False),
                "playCount": ud.get("PlayCount", 0), "lastPlayedDate": ud.get("LastPlayedDate"), "playedPercentage": round(pct, 1)})
    result = sorted(all_eps.values(), key=lambda x: x["indexNumber"])
    return jsonify(result)

@app.route("/api/watch-status/<item_id>")
@login_required_api
def api_watch_status(item_id):
    users = jf_get("/Users")
    out = []
    for u in users:
        try:
            item = jf_get(f'/Users/{u["Id"]}/Items/{item_id}')
            ud = item.get("UserData", {})
            pct = ud.get("PlayedPercentage", 0) or 0
            if ud.get("Played", False) and pct == 0: pct = 100.0
            out.append({"userId": u["Id"], "userName": u["Name"], "played": ud.get("Played", False),
                "playCount": ud.get("PlayCount", 0), "lastPlayedDate": ud.get("LastPlayedDate"), "playedPercentage": round(pct, 1)})
        except:
            out.append({"userId": u["Id"], "userName": u["Name"], "played": False, "playCount": 0, "lastPlayedDate": None, "playedPercentage": 0})
    return jsonify(out)

@app.route("/api/recent/movies")
@login_required_api
def api_recent_movies():
    users = jf_get("/Users")
    if not users:
        return jsonify([])
    all_items = []
    for user in users:
        params = {
            "Limit": 20,
            "SortBy": "DatePlayed",
            "SortOrder": "Descending",
            "IncludeItemTypes": "Movie",
            "Recursive": "true",
            "Fields": "PrimaryImageAspectRatio,BasicSyncInfo"
        }
        try:
            data = jf_get(f"/Users/{user['Id']}/Items", params)
            for i in data.get("Items", []):
                last_played = i.get("UserData", {}).get("LastPlayedDate")
                if last_played:
                    all_items.append({
                        "id": i["Id"],
                        "name": i["Name"],
                        "year": i.get("ProductionYear"),
                        "imageUrl": f"/api/image/{i['Id']}?type=Primary&maxWidth=200",
                        "lastPlayedDate": last_played
                    })
        except:
            continue
    all_items.sort(key=lambda x: x["lastPlayedDate"], reverse=True)
    return jsonify(all_items[:10])

@app.route("/api/recent/episodes")
@login_required_api
def api_recent_episodes():
    users = jf_get("/Users")
    if not users:
        return jsonify([])
    all_items = []
    for user in users:
        params = {
            "Limit": 20,
            "SortBy": "DatePlayed",
            "SortOrder": "Descending",
            "IncludeItemTypes": "Episode",
            "Recursive": "true",
            "Fields": "PrimaryImageAspectRatio,BasicSyncInfo,SeriesName,SeasonName,ParentId,SeriesId"
        }
        try:
            data = jf_get(f"/Users/{user['Id']}/Items", params)
            for i in data.get("Items", []):
                last_played = i.get("UserData", {}).get("LastPlayedDate")
                if last_played:
                    all_items.append({
                        "id": i["Id"],
                        "name": i["Name"],
                        "seriesName": i.get("SeriesName", ""),
                        "seasonName": i.get("SeasonName", ""),
                        "episodeNumber": i.get("IndexNumber"),
                        "imageUrl": f"/api/image/{i.get('SeriesId', i['Id'])}?type=Primary&maxWidth=200",
                        "lastPlayedDate": last_played,
                        "seriesId": i.get("SeriesId")
                    })
        except:
            continue
    all_items.sort(key=lambda x: x["lastPlayedDate"], reverse=True)
    return jsonify(all_items[:10])

@app.route("/api/assignments/<item_id>")
@login_required_api
def api_get_assignments(item_id):
    assigned = get_assigned_ids(item_id)
    if assigned is None:
        return jsonify({"assigned": [], "mode": "all"})
    return jsonify({"assigned": list(assigned), "mode": "custom"})

@app.route("/api/assignments/<item_id>", methods=["POST"])
@login_required_api
def api_set_assignments(item_id):
    user_ids = request.json.get("userIds", [])
    if not set_assignment_by_provider(item_id, user_ids):
        set_assignment_by_item_id(item_id, user_ids)
    return jsonify({"success": True})

@app.route("/api/assignments/<item_id>", methods=["DELETE"])
@login_required_api
def api_delete_assignments(item_id):
    delete_assignment_by_provider(item_id)
    delete_assignment_by_item_id(item_id)
    return jsonify({"success": True})

# Bulk assignment endpoint
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
    
    success_count = 0
    failed_ids = []
    for item_id in item_ids:
        try:
            if not set_assignment_by_provider(item_id, user_ids):
                set_assignment_by_item_id(item_id, user_ids)
            success_count += 1
            app.logger.info(f"Bulk assigned users to {item_id} from {request.remote_addr}")
        except Exception as e:
            failed_ids.append({"id": item_id, "error": str(e)})
    
    return jsonify({
        "success": True,
        "assigned_count": success_count,
        "failed": failed_ids
    })

@app.route("/api/delete/<item_id>", methods=["DELETE"])
@login_required_api
def api_delete_item(item_id):
    try:
        jf_delete(f"/Items/{item_id}")
        app.logger.info(f"Deleted item {item_id} from {request.remote_addr}")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/delete-batch", methods=["DELETE"])
@login_required_api
def api_delete_batch():
    ids = request.json.get("itemIds", [])
    deleted, failed = [], []
    for iid in ids:
        try:
            jf_delete(f"/Items/{iid}")
            deleted.append(iid)
            app.logger.info(f"Deleted batch item {iid} from {request.remote_addr}")
        except Exception as e:
            failed.append({"id": iid, "error": str(e)})
    return jsonify({"success": True, "deleted": deleted, "failed": failed})

@app.route("/api/check-season-empty/<series_id>/<season_id>")
@login_required_api
def api_check_season_empty(series_id, season_id):
    users = jf_get("/Users")
    uid = users[0]["Id"] if users else None
    if not uid: return jsonify({"empty": False})
    try:
        data = jf_get(f"/Shows/{series_id}/Episodes", {"SeasonId": season_id, "userId": uid})
        return jsonify({"empty": len(data.get("Items", [])) == 0})
    except: return jsonify({"empty": False})

@app.route("/api/image/<item_id>")
@limiter.exempt
@login_required_api
def proxy_image(item_id):
    try:
        r = jf_get_raw(f"/Items/{item_id}/Images/{request.args.get('type','Primary')}", {"maxWidth": request.args.get("maxWidth","300"), "quality": "80"})
        return Response(r.iter_content(8192), content_type=r.headers.get("Content-Type","image/jpeg"), headers={"Cache-Control": "public, max-age=86400"})
    except: return Response(status=404)

if __name__ == "__main__":
    if not JELLYFIN_API_KEY:
        print("Error: JELLYFIN_API_KEY is required\n\nUsage:\n  JELLYFIN_URL=http://127.0.0.1:8096 JELLYFIN_API_KEY=key PORT=8080 python app.py"); sys.exit(1)
    
    admin = get_admin()
    if not admin or not admin.get("password_hash") or not admin.get("username"):
        print("\n" + "="*50)
        print("First time setup: Create admin username and password")
        print("="*50)
        import getpass
        username = input("Username (default 'admin'): ").strip() or "admin"
        pwd = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if pwd != confirm:
            print("Passwords do not match. Exiting.")
            sys.exit(1)
        salt = bcrypt.gensalt()
        pwd_hash = bcrypt.hashpw(pwd.encode('utf-8'), salt).decode('utf-8')
        set_admin(username, pwd_hash)
        print(f"Admin user '{username}' created.\n")
    
    init_db()
    if os.path.exists(CONFIG_PATH):
        os.chmod(CONFIG_PATH, 0o600)
    print(f"Jellyfin Watch History Dashboard (with login)\n  Jellyfin: {JELLYFIN_URL}\n  Port:     {PORT}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)