"""Unit tests for the pure logic that gates watch-state and deletion.

These functions need no Jellyfin server or DB connection, so they're cheap to
test and exactly the spots where a regression would silently mis-count watches
or delete the wrong media.
"""
import bcrypt

import config_store
from jellyfin_api import parse_jellyfin_providers
from webhook_state import is_item_watched_pwe, get_last_played_pwe
from assignments import _resolve_target


# ── parse_jellyfin_providers ──────────────────────────────────────────
def test_parse_jellyfin_providers_basic():
    pids = {"Tmdb": 123, "Imdb": "tt9", "Tvdb": "77"}
    assert parse_jellyfin_providers(pids) == {"Tmdb": "123", "Imdb": "tt9", "Tvdb": "77"}


def test_parse_jellyfin_providers_ignores_unknown_and_empty():
    assert parse_jellyfin_providers({}) == {}
    assert parse_jellyfin_providers(None) == {}
    assert parse_jellyfin_providers({"Foo": "bar", "Tmdb": ""}) == {}


# ── is_item_watched_pwe (ALL target accounts must have watched) ────────
def test_is_item_watched_pwe_requires_every_account():
    providers = {"Tmdb": "1"}
    played = {("tmdb", "1", "movie"): {"a", "b"}}
    assert is_item_watched_pwe(providers, "movie", {"a"}, played) is True
    assert is_item_watched_pwe(providers, "movie", {"a", "b"}, played) is True
    assert is_item_watched_pwe(providers, "movie", {"a", "c"}, played) is False


def test_is_item_watched_pwe_empty_inputs_are_false():
    assert is_item_watched_pwe({}, "movie", {"a"}, {}) is False
    assert is_item_watched_pwe({"Tmdb": "1"}, "movie", set(), {}) is False


def test_is_item_watched_pwe_matches_on_any_provider():
    # A watch recorded under Imdb should still count when the item also has Tmdb.
    providers = {"Tmdb": "1", "Imdb": "tt1"}
    played = {("imdb", "tt1", "episode"): {"a"}}
    assert is_item_watched_pwe(providers, "episode", {"a"}, played) is True


# ── get_last_played_pwe ───────────────────────────────────────────────
def test_get_last_played_pwe_picks_latest_across_providers():
    providers = {"Tmdb": "1", "Imdb": "tt1"}
    played_ts = {
        ("tmdb", "1", "episode", "a"): "2024-01-01T00:00:00+00:00",
        ("imdb", "tt1", "episode", "a"): "2024-02-01T00:00:00+00:00",
    }
    assert get_last_played_pwe(providers, "episode", "a", played_ts) == "2024-02-01T00:00:00+00:00"
    assert get_last_played_pwe(providers, "episode", "z", played_ts) is None


# ── _resolve_target (provider assignment > item assignment > everyone) ─
def test_resolve_target_provider_assignment_wins():
    provider_assignments = {("Tmdb", "1"): {"u1"}}
    item_assignments = {"rk1": {"u2"}}
    all_ids = {"u1", "u2", "u3"}
    assert _resolve_target({"Tmdb": "1"}, "rk1", item_assignments,
                           provider_assignments, all_ids) == {"u1"}


def test_resolve_target_falls_back_to_item_assignment():
    item_assignments = {"rk1": {"u2"}}
    all_ids = {"u1", "u2"}
    assert _resolve_target({}, "rk1", item_assignments, {}, all_ids) == {"u2"}


def test_resolve_target_defaults_to_all_when_unassigned():
    all_ids = {"u1", "u2"}
    assert _resolve_target({}, "rkX", {}, {}, all_ids) == all_ids


# ── verify_admin (constant-time-ish, never grants without a real hash) ─
def test_verify_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(tmp_path / "config.json"))
    config_store._cfg_cache["mtime"] = None
    config_store._cfg_cache["data"] = None
    h = bcrypt.hashpw(b"correcthorse", bcrypt.gensalt()).decode("utf-8")
    config_store.save_config({"admin": {"username": "admin", "password_hash": h}})

    assert config_store.verify_admin("admin", "correcthorse") is True
    assert config_store.verify_admin("admin", "wrong") is False
    assert config_store.verify_admin("nope", "correcthorse") is False
    assert config_store.verify_admin("admin", "") is False


def test_verify_admin_no_admin_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(tmp_path / "missing.json"))
    config_store._cfg_cache["mtime"] = None
    config_store._cfg_cache["data"] = None
    # No admin record: must never authenticate, even against the dummy hash.
    assert config_store.verify_admin("admin", "invalid") is False
