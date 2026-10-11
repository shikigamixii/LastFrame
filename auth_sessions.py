"""Server-side record of admin login sessions.

Flask's session cookie is signed but stateless, so on its own a copied cookie
keeps working after logout or a password change. Each login now gets a random
session id kept in the cookie, and only its SHA-256 is stored here (a leaked
database can't be replayed as live sessions). A cookie is accepted only while
its id is on record and within both time limits:

    IDLE_SECONDS      - ends after this long without any request
    ABSOLUTE_SECONDS  - ends this long after login, however active
"""
import hashlib
import secrets
import time

from db import get_db

IDLE_SECONDS = 7 * 24 * 3600
ABSOLUTE_SECONDS = 30 * 24 * 3600
# Refresh last_seen at most this often, so busy pages don't write on every request.
_TOUCH_INTERVAL = 60


def _digest(sid):
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


def create():
    """Record a new session and return its id (store it in the cookie)."""
    sid = secrets.token_urlsafe(32)
    now = time.time()
    db = get_db()
    try:
        db.execute("DELETE FROM auth_sessions WHERE last_seen < ? OR created_at < ?",
                   (now - IDLE_SECONDS, now - ABSOLUTE_SECONDS))
        db.execute("INSERT INTO auth_sessions (sid_hash, created_at, last_seen) VALUES (?, ?, ?)",
                   (_digest(sid), now, now))
        db.commit()
    finally:
        db.close()
    return sid


def is_valid(sid):
    """True while the session is on record and inside both time limits."""
    if not sid or not isinstance(sid, str):
        return False
    now = time.time()
    h = _digest(sid)
    db = get_db()
    try:
        row = db.execute("SELECT created_at, last_seen FROM auth_sessions WHERE sid_hash=?", (h,)).fetchone()
        if row is None:
            return False
        if now - row["last_seen"] > IDLE_SECONDS or now - row["created_at"] > ABSOLUTE_SECONDS:
            db.execute("DELETE FROM auth_sessions WHERE sid_hash=?", (h,))
            db.commit()
            return False
        if now - row["last_seen"] > _TOUCH_INTERVAL:
            db.execute("UPDATE auth_sessions SET last_seen=? WHERE sid_hash=?", (now, h))
            db.commit()
        return True
    finally:
        db.close()


def revoke(sid):
    if not sid or not isinstance(sid, str):
        return
    db = get_db()
    try:
        db.execute("DELETE FROM auth_sessions WHERE sid_hash=?", (_digest(sid),))
        db.commit()
    finally:
        db.close()


def revoke_all():
    """End every session (used when the admin credentials change)."""
    db = get_db()
    try:
        db.execute("DELETE FROM auth_sessions")
        db.commit()
    finally:
        db.close()
