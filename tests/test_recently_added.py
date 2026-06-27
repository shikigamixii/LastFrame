"""Tests for the Recently Added triage list.

Cover the pure helpers plus the DB-backed read/handle logic that the home
page and the assignment hook rely on. No Plex server is needed: discover()
is exercised indirectly through direct row inserts, and mark_handled's
provider lookup is monkeypatched.
"""
from datetime import datetime, timezone, timedelta

import recently_added as ra
from db import get_db, init_db


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _reset_and_insert(rows):
    init_db()
    db = get_db()
    db.execute("DELETE FROM recently_added")
    for r in rows:
        db.execute(
            "INSERT OR REPLACE INTO recently_added "
            "(provider_type, provider_id, item_type, library_id, rating_key, title, year, first_seen, handled, handled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (r["ptype"], r["pid"], r.get("item_type", "movie"), "1",
             r.get("rk", r["pid"]), r.get("title", "X"), 2020,
             r["first_seen"], r.get("handled", 0)))
    db.commit()
    db.close()


# ── window_days ───────────────────────────────────────────────────────
def test_window_days_default_and_parse():
    assert ra.window_days({}) == 30
    assert ra.window_days({"recently_added_window_days": 7}) == 7
    assert ra.window_days({"recently_added_window_days": 0}) == 30   # 0 → default
    assert ra.window_days({"recently_added_window_days": "bad"}) == 30


# ── _provider_key (provider id preferred, rating key fallback) ─────────
def test_provider_key_prefers_provider_then_ratingkey():
    assert ra._provider_key({"Tmdb": "5"}, "rk1") == ("Tmdb", "5")
    assert ra._provider_key({"Imdb": "tt9"}, "rk1") == ("Imdb", "tt9")
    assert ra._provider_key({}, "rk1") == ("RatingKey", "rk1")


# ── _monitored_sections ───────────────────────────────────────────────
def test_monitored_sections_respects_scope():
    sections = [{"key": "1", "type": "movie"}, {"key": "2", "type": "show"},
                {"key": "3", "type": "artist"}]
    assert ra._monitored_sections({"show_all_libraries": True}, sections) == [("1", "movie"), ("2", "show")]
    scoped = ra._monitored_sections(
        {"show_all_libraries": False, "monitored_libraries": ["2"]}, sections)
    assert scoped == [("2", "show")]


# ── get_recent (window + handled filters, newest first) ───────────────
def test_get_recent_filters_and_orders():
    _reset_and_insert([
        {"ptype": "Tmdb", "pid": "1", "rk": "1", "title": "new", "first_seen": _iso(1)},
        {"ptype": "Tmdb", "pid": "2", "rk": "2", "title": "old", "first_seen": _iso(99)},
        {"ptype": "Tmdb", "pid": "3", "rk": "3", "title": "handled", "first_seen": _iso(1), "handled": 1},
        {"ptype": "Tmdb", "pid": "4", "rk": "4", "title": "newer", "first_seen": _iso(0)},
    ])
    items = ra.get_recent({"recently_added_window_days": 30}, limit=100)
    assert [i["name"] for i in items] == ["newer", "new"]


def test_get_recent_respects_limit():
    _reset_and_insert([
        {"ptype": "Tmdb", "pid": str(i), "rk": str(i), "title": f"t{i}", "first_seen": _iso(i)}
        for i in range(5)
    ])
    assert len(ra.get_recent({"recently_added_window_days": 365}, limit=2)) == 2


# ── mark_handled (drops the title off the list) ───────────────────────
def test_mark_handled_by_rating_key(monkeypatch):
    monkeypatch.setattr(ra, "get_item_providers", lambda i: {})
    _reset_and_insert([{"ptype": "RatingKey", "pid": "77", "rk": "77", "first_seen": _iso(1)}])
    ra.mark_handled("77")
    assert ra.get_recent({}, limit=100) == []


def test_mark_handled_by_provider_when_rating_key_changed(monkeypatch):
    # Row keyed by provider; rating key has since changed in Plex. Matching on
    # the resolved provider id must still flag it.
    monkeypatch.setattr(ra, "get_item_providers", lambda i: {"Tmdb": "9"})
    _reset_and_insert([{"ptype": "Tmdb", "pid": "9", "rk": "old-rk", "first_seen": _iso(1)}])
    ra.mark_handled("brand-new-rk")
    assert ra.get_recent({}, limit=100) == []
