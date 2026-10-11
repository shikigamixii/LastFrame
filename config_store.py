"""Config and admin record helpers — load/save JSON, admin credentials."""
import os, sys, json, copy, hmac, threading
import bcrypt

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", APP_DIR)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "data.db")
PAGE_SIZE = 50
PROVIDER_PRIORITY = ["Tmdb", "Imdb", "Tvdb"]

def _default_config():
    return {"monitored_libraries": [], "show_all_libraries": True,
            # Per-provider master switches. A provider is only queried when its
            # flag is on AND its credentials are configured, so users can run
            # both Plex and Jellyfin at once or toggle either off at any time.
            "plex_enabled": False, "jellyfin_enabled": False,
            "auto_delete_enabled": False, "auto_delete_libraries": [],
            "auto_delete_grace_days": 1, "auto_delete_min_delay_minutes": 30,
            "auto_delete_library_enabled_at": {},
            "recently_added_window_days": 30}

# config.json is read on the hot path — every Jellyfin call resolves the URL and
# api key through load_config(). Cache the parsed dict and only re-read when the
# file's mtime changes. Guarded by a lock because background sweep threads also
# read it. load_config() returns a deep copy so callers that mutate the dict
# (then save_config) can't corrupt the cached copy.
_cfg_lock = threading.Lock()
_cfg_cache = {"mtime": None, "data": None}

def check_data_dir(exit_code=1):
    """Exit with a clear message if DATA_DIR or its files can't be read and
    written by the user the app runs as (e.g. files left by an older
    root-run container). Starting anyway could misread the config as a fresh
    install and reopen first-time setup."""
    problems = []
    if not os.path.isdir(DATA_DIR):
        problems.append(f"{DATA_DIR} does not exist or is not a folder")
    elif not os.access(DATA_DIR, os.R_OK | os.W_OK | os.X_OK):
        problems.append(f"{DATA_DIR} (the folder itself)")
    else:
        for name in ("config.json", "data.db", "data.db-wal", "data.db-shm", "audit.log"):
            path = os.path.join(DATA_DIR, name)
            if os.path.exists(path) and not os.access(path, os.R_OK | os.W_OK):
                problems.append(path)
    if not problems:
        return
    uid, gid = os.getuid(), os.getgid()
    try:
        st = os.stat(DATA_DIR)
        owner = f"; the folder is owned by {st.st_uid}:{st.st_gid}"
    except OSError:
        owner = ""
    sys.stderr.write(
        "\nLastFrame cannot start: it can't read and write its data.\n"
        + "".join(f"  - {p}\n" for p in problems)
        + f"The app is running as user {uid}:{gid}{owner}.\n"
        "Fix: give the data folder to that user on the host, e.g.\n"
        f"  sudo chown -R {uid}:{gid} <your data folder>\n"
        "or set PUID/PGID in .env to the folder's owner (see README).\n\n")
    sys.stderr.flush()
    raise SystemExit(exit_code)


def restrict_data_files():
    """New files (audit.log, data.db and its -wal/-shm, config.json) are
    created owner-only, and existing ones are tightened to 0600. They hold
    tokens, password hashes, watch history and IP addresses."""
    os.umask(0o077)
    for name in ("config.json", "data.db", "data.db-wal", "data.db-shm", "audit.log"):
        path = os.path.join(DATA_DIR, name)
        try:
            if os.stat(path).st_uid == os.getuid():
                os.chmod(path, 0o600)
        except OSError:
            pass


class ConfigUnreadableError(RuntimeError):
    """config.json exists but can't be read or parsed."""


def load_config():
    # Only a config file that genuinely doesn't exist means "fresh install".
    # One that exists but can't be read (wrong owner/permissions) or parsed
    # must fail loudly: falling back to defaults would look like "no admin"
    # and reopen the unauthenticated first-time setup to anyone.
    with _cfg_lock:
        try:
            mtime = os.stat(CONFIG_PATH).st_mtime
        except FileNotFoundError:
            mtime = None
        except OSError as e:
            raise ConfigUnreadableError(f"Cannot access {CONFIG_PATH}: {e}") from e
        if mtime != _cfg_cache["mtime"] or _cfg_cache["data"] is None:
            if mtime is None:
                _cfg_cache["data"] = _default_config()
            else:
                try:
                    with open(CONFIG_PATH) as f:
                        _cfg_cache["data"] = json.load(f)
                except (OSError, ValueError) as e:
                    raise ConfigUnreadableError(f"Cannot read {CONFIG_PATH}: {e}") from e
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
