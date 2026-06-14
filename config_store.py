"""Config and admin record helpers — load/save JSON, admin credentials."""
import os, json
import bcrypt

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", APP_DIR)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "data.db")
PAGE_SIZE = 50
PROVIDER_PRIORITY = ["Tmdb", "Imdb", "Tvdb"]

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f: return json.load(f)
    return {"monitored_libraries": [], "show_all_libraries": True,
            "auto_delete_enabled": False, "auto_delete_libraries": [],
            "auto_delete_grace_days": 1, "auto_delete_min_delay_minutes": 30,
            "auto_delete_library_enabled_at": {}}

def save_config(config):
    with open(CONFIG_PATH, "w") as f: json.dump(config, f, indent=2)
    if os.path.exists(CONFIG_PATH): os.chmod(CONFIG_PATH, 0o600)

def get_admin():
    cfg = load_config()
    return cfg.get("admin", {})

def set_admin(username, password_hash):
    cfg = load_config()
    cfg["admin"] = {"username": username, "password_hash": password_hash}
    save_config(cfg)

def verify_admin(username, password):
    admin = get_admin()
    if not admin: return False
    if username != admin.get("username"): return False
    pwd_hash = admin.get("password_hash")
    if not pwd_hash: return False
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

def is_setup_needed():
    admin = get_admin()
    return not (admin and admin.get("username") and admin.get("password_hash"))
