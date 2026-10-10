"""Tests for the Recently Added triage list.

Cover the pure helpers plus the DB-backed read/handle logic that the home
page and the assignment hook rely on. No Jellyfin server is needed:
discover() is exercised indirectly through direct row inserts, and
mark_handled's provider lookup is monkeypatched.
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


# ── _provider_key (provider id preferred, item id fallback) ───────────
def test_provider_key_prefers_provider_then_ratingkey():
    assert ra._provider_key({"Tmdb": "5"}, "rk1") == ("Tmdb", "5")
    assert ra._provider_key({"Imdb": "tt9"}, "rk1") == ("Imdb", "tt9")
    assert ra._provider_key({}, "rk1") == ("RatingKey", "rk1")


# ── _norm_created (Jellyfin DateCreated → canonical UTC ISO) ──────────
# Moved into the Jellyfin engine as part of the multi-provider split.
def test_norm_created_handles_z_and_long_fractions():
    import jellyfin_engine as je
    out = je._norm_created("2024-06-01T12:34:56.1234567Z")
    assert out is not None and out.endswith("+00:00") and out.startswith("2024-06-01T12:34:56")
    assert je._norm_created("") is None
    assert je._norm_created("not-a-date") is None


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


def test_mark_handled_by_provider_when_item_id_changed(monkeypatch):
    # Row keyed by provider; the item id has since changed in Jellyfin. Matching
    # on the resolved provider id must still flag it.
    monkeypatch.setattr(ra, "get_item_providers", lambda i: {"Tmdb": "9"})
    _reset_and_insert([{"ptype": "Tmdb", "pid": "9", "rk": "old-id", "first_seen": _iso(1)}])
    ra.mark_handled("brand-new-id")
    assert ra.get_recent({}, limit=100) == []


# ── discover (refreshes a stale item id without touching first_seen) ─
class _FakeEngine:
    def __init__(self, key, rows):
        self.KEY = key
        self._rows = rows

    def recently_added_scan(self, cfg):
        return self._rows


def _discover_with(monkeypatch, engines):
    monkeypatch.setattr(ra._providers, "enabled_engines", lambda cfg=None: engines)
    return ra.discover({})


def _row(pid):
    db = get_db()
    r = db.execute("SELECT * FROM recently_added WHERE provider_type='Tmdb' AND provider_id=?", (pid,)).fetchone()
    db.close()
    return r


def test_discover_refreshes_rekeyed_item(monkeypatch):
    seen = _iso(5)
    _reset_and_insert([{"ptype": "Tmdb", "pid": "7", "rk": "px_100", "first_seen": seen, "handled": 1}])
    eng = _FakeEngine("plex", [{"providers": {"Tmdb": "7"}, "item_type": "movie",
                                "library_id": "px_2", "rating_key": "px_200", "first_seen": _iso(0)}])
    assert _discover_with(monkeypatch, [eng]) == 0
    r = _row("7")
    assert r["rating_key"] == "px_200" and r["library_id"] == "px_2"
    assert r["first_seen"] == seen and r["handled"] == 1


def test_discover_fixes_legacy_unprefixed_id(monkeypatch):
    _reset_and_insert([{"ptype": "Tmdb", "pid": "8", "rk": "100", "first_seen": _iso(5)}])
    eng = _FakeEngine("plex", [{"providers": {"Tmdb": "8"}, "item_type": "movie",
                                "library_id": "px_1", "rating_key": "px_100"}])
    _discover_with(monkeypatch, [eng])
    assert _row("8")["rating_key"] == "px_100"


def test_discover_does_not_steal_other_providers_row(monkeypatch):
    _reset_and_insert([{"ptype": "Tmdb", "pid": "9", "rk": "px_100", "first_seen": _iso(5)}])
    jf = _FakeEngine("jellyfin", [{"providers": {"Tmdb": "9"}, "item_type": "movie",
                                   "library_id": "jf_abc", "rating_key": "jf_def"}])
    _discover_with(monkeypatch, [jf])
    assert _row("9")["rating_key"] == "px_100"


# ── prune_missing (drops titles deleted from the server) ──────────────
class _ExistsEngine:
    def __init__(self, key, existing=None, fail=False):
        self.KEY = key
        self._existing = set(existing or [])
        self._fail = fail

    def existing_raw_ids(self, raws):
        if self._fail:
            raise RuntimeError("server unreachable")
        return {r for r in raws if r in self._existing}


def _handled(pid):
    return _row(pid)["handled"]


def test_prune_marks_missing_items_handled(monkeypatch):
    _reset_and_insert([
        {"ptype": "Tmdb", "pid": "1", "rk": "px_1", "first_seen": _iso(1)},
        {"ptype": "Tmdb", "pid": "2", "rk": "px_2", "first_seen": _iso(1)},
        {"ptype": "Tmdb", "pid": "3", "rk": "jf_aa", "first_seen": _iso(1)},
    ])
    engines = [_ExistsEngine("plex", existing={"1"}), _ExistsEngine("jellyfin", existing={"aa"})]
    monkeypatch.setattr(ra._providers, "enabled_engines", lambda cfg=None: engines)
    assert ra.prune_missing({}) == 1
    assert (_handled("1"), _handled("2"), _handled("3")) == (0, 1, 0)


def test_prune_skips_engine_when_check_fails(monkeypatch):
    _reset_and_insert([{"ptype": "Tmdb", "pid": "1", "rk": "px_1", "first_seen": _iso(1)}])
    monkeypatch.setattr(ra._providers, "enabled_engines",
                        lambda cfg=None: [_ExistsEngine("plex", fail=True)])
    assert ra.prune_missing({}) == 0
    assert _handled("1") == 0


def test_prune_ignores_disabled_provider_and_legacy_rows(monkeypatch):
    _reset_and_insert([
        {"ptype": "Tmdb", "pid": "1", "rk": "jf_aa", "first_seen": _iso(1)},
        {"ptype": "Tmdb", "pid": "2", "rk": "12345", "first_seen": _iso(1)},
    ])
    monkeypatch.setattr(ra._providers, "enabled_engines",
                        lambda cfg=None: [_ExistsEngine("plex", existing=set())])
    assert ra.prune_missing({}) == 0
    assert (_handled("1"), _handled("2")) == (0, 0)


def test_mark_handled_without_provider_lookup(monkeypatch):
    _reset_and_insert([{"ptype": "Tmdb", "pid": "1", "rk": "px_1", "first_seen": _iso(1)}])
    def boom(_):
        raise AssertionError("provider lookup should be skipped")
    monkeypatch.setattr(ra, "get_item_providers", boom)
    ra.mark_handled("px_1", lookup_providers=False)
    assert _handled("1") == 1


# ── engine existence checks (an outage must never read as "deleted") ──
def _http_error(code):
    import requests
    r = requests.Response(); r.status_code = code
    return requests.HTTPError(str(code), response=r)


def test_plex_existing_raw_ids(monkeypatch):
    import plex_engine as pe
    monkeypatch.setattr(pe, "plex_get", lambda path, params=None: {"size": 1, "Metadata": [{"ratingKey": "5"}]})
    assert pe.existing_raw_ids(["5", "6"]) == {"5"}

    def all_gone(path, params=None):
        raise _http_error(404)
    monkeypatch.setattr(pe, "plex_get", all_gone)
    assert pe.existing_raw_ids(["6"]) == set()

    def outage(path, params=None):
        raise _http_error(503)
    monkeypatch.setattr(pe, "plex_get", outage)
    import pytest
    with pytest.raises(Exception):
        pe.existing_raw_ids(["5"])
    monkeypatch.setattr(pe, "plex_get", lambda path, params=None: {})
    with pytest.raises(Exception):
        pe.existing_raw_ids(["5"])


def test_jellyfin_existing_raw_ids(monkeypatch):
    import pytest
    import jellyfin_engine as je
    monkeypatch.setattr(je, "jellyfin_get", lambda path, params=None: {"Items": [{"Id": "aa"}]})
    assert je.existing_raw_ids(["aa", "bb"]) == {"aa"}
    monkeypatch.setattr(je, "jellyfin_get", lambda path, params=None: {})
    with pytest.raises(Exception):
        je.existing_raw_ids(["aa"])
