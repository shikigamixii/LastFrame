"""Config and admin record helpers — load/save JSON, admin credentials."""
import os, json, copy, hmac, threading
import bcrypt

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", APP_DIR)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "data.db")
PAGE_SIZE = 50
PROVIDER_PRIORITY = ["Tmdb", "Imdb", "Tvdb"]

def _default_config():
    return {"monitored_libraries": [], "show_all_libraries": True,
            "auto_delete_enabled": False, "auto_delete_libraries": [],
            "auto_delete_grace_days": 1, "auto_delete_min_delay_minutes": 30,
            "auto_delete_library_enabled_at": {},
            "recently_added_window_days": 30}

# config.json is read on the hot path — every Plex call resolves the URL and
# token through load_config(). Cache the parsed dict and only re-read when the
# file's mtime changes. Guarded by a lock because background sweep threads also
# read it. load_config() returns a deep copy so callers that mutate the dict
# (then save_config) can't corrupt the cached copy.
_cfg_lock = threading.Lock()
_cfg_cache = {"mtime": None, "data": None}

def load_config():
    with _cfg_lock:
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            mtime = None
        if mtime != _cfg_cache["mtime"] or _cfg_cache["data"] is None:
            if mtime is None:
                _cfg_cache["data"] = _default_config()
            else:
                try:
                    with open(CONFIG_PATH) as f:
                        _cfg_cache["data"] = json.load(f)
                except (OSError, ValueError):
                    _cfg_cache["data"] = _default_config()
            _cfg_cache["mtime"] = mtime
        return copy.deepcopy(_cfg_cache["data"])

def save_config(config):
    with open(CONFIG_PATH, "w") as f: json.dump(config, f, indent=2)
    if os.path.exists(CONFIG_PATH): os.chmod(CONFIG_PATH, 0o600)
    with _cfg_lock:
        # Refresh the cache from what we just wrote so the next read is current
        # even if the filesystem's mtime resolution is coarse.
        _cfg_cache["data"] = copy.deepcopy(config)
        try:
            _cfg_cache["mtime"] = os.path.getmtime(CONFIG_PATH)
        except OSError:
            _cfg_cache["mtime"] = None

def get_admin():
    cfg = load_config()
    return cfg.get("admin", {})

def set_admin(username, password_hash):
    cfg = load_config()
    cfg["admin"] = {"username": username, "password_hash": password_hash}
    save_config(cfg)

# A throwaway hash so verify_admin can always run a bcrypt comparison even when
# no admin (or no stored hash) exists — keeps response timing from revealing
# whether a username is valid.
_DUMMY_HASH = bcrypt.hashpw(b"invalid", bcrypt.gensalt()).decode("utf-8")

def verify_admin(username, password):
    admin = get_admin()
    real_hash = (admin or {}).get("password_hash")
    stored = real_hash or _DUMMY_HASH
    try:
        pwd_ok = bcrypt.checkpw((password or "").encode("utf-8"), stored.encode("utf-8"))
    except (ValueError, TypeError):
        pwd_ok = False
    user_ok = bool(admin) and hmac.compare_digest((username or ""), (admin.get("username") or ""))
    # bool(real_hash) ensures a dummy-hash match can never grant access.
    return bool(real_hash) and pwd_ok and user_ok

def update_admin_credentials(current_username, current_password, new_username, new_password):
    if not verify_admin(current_username, current_password):
        return False, "Current credentials are incorrect"
    if not new_username or not new_password:
        return False, "Username and password cannot be empty"
    salt = bcrypt.gensalt()
    new_hash = bcrypt.hashpw(new_password.encode('utf-8'), salt).decode('utf-8')
    set_admin(new_username, new_hash)
    return True, "Credentials updated"

def is_setup_needed():
    admin = get_admin()
    return not (admin and admin.get("username") and admin.get("password_hash"))
