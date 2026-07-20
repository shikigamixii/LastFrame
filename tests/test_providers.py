"""Tests for the multi-provider abstraction: id namespacing, engine identity,
and provider dispatch. No Plex/Jellyfin server is required — these exercise the
pure helpers that keep the two providers' ids from colliding.
"""
import idutil
import providers
import plex_engine
import jellyfin_engine


# ── idutil (namespacing round-trips) ──────────────────────────────────
def test_make_id_uses_short_prefix():
    assert idutil.make_id("plex", "12345") == "px_12345"
    assert idutil.make_id("jellyfin", "9a8b") == "jf_9a8b"


def test_split_id_round_trips():
    assert idutil.split_id("px_12345") == ("plex", "12345")
    assert idutil.split_id("jf_9a8b7c6d") == ("jellyfin", "9a8b7c6d")


def test_split_id_leaves_hex_ids_intact():
    # Jellyfin ids are hex with no underscore, so only the leading prefix splits.
    raw = "0123456789abcdef0123456789abcdef"
    assert idutil.split_id(idutil.make_id("jellyfin", raw)) == ("jellyfin", raw)


def test_split_id_unknown_prefix_is_bare():
    assert idutil.split_id("zz_1") == (None, "zz_1")
    assert idutil.split_id("12345") == (None, "12345")
    assert idutil.key_of("12345") is None


# ── engine identity ───────────────────────────────────────────────────
def test_engine_keys_and_labels():
    assert plex_engine.KEY == "plex" and plex_engine.LABEL == "Plex"
    assert jellyfin_engine.KEY == "jellyfin" and jellyfin_engine.LABEL == "Jellyfin"


def test_valid_raw_matches_expected_id_shapes():
    assert plex_engine.valid_raw("12345") is True
    assert plex_engine.valid_raw("abc") is False          # Plex ids are numeric
    assert jellyfin_engine.valid_raw("0123456789abcdef0123456789abcdef") is True
    assert jellyfin_engine.valid_raw("12") is True         # legacy numeric ids allowed
    assert jellyfin_engine.valid_raw("bad id!") is False


# ── registry / dispatch ───────────────────────────────────────────────
def test_registry_lists_both_engines_in_order():
    assert [e.KEY for e in providers.all_engines()] == ["plex", "jellyfin"]
    assert providers.get_engine("plex") is plex_engine
    assert providers.get_engine("jellyfin") is jellyfin_engine
    assert providers.get_engine("nope") is None


def test_engine_for_id_dispatches_by_prefix():
    # Pure prefix dispatch, independent of whether the provider is configured.
    e, raw = providers.engine_for_id("px_77", require_enabled=False)
    assert e is plex_engine and raw == "77"
    e, raw = providers.engine_for_id("jf_abcd", require_enabled=False)
    assert e is jellyfin_engine and raw == "abcd"


def test_engine_for_id_respects_enabled_toggle(monkeypatch):
    cfg = {"plex_enabled": False, "jellyfin_enabled": True}
    # Plex configured but disabled -> not resolvable when require_enabled.
    monkeypatch.setattr(plex_engine, "is_configured", lambda: True)
    monkeypatch.setattr(jellyfin_engine, "is_configured", lambda: True)
    assert providers.engine_for_id("px_1", cfg)[0] is None
    assert providers.engine_for_id("jf_1", cfg)[0] is jellyfin_engine
    # But still resolvable when enable is not required (e.g. webhook storage).
    assert providers.engine_for_id("px_1", cfg, require_enabled=False)[0] is plex_engine


def test_enabled_engines_filters_on_flag_and_config(monkeypatch):
    monkeypatch.setattr(plex_engine, "is_configured", lambda: True)
    monkeypatch.setattr(jellyfin_engine, "is_configured", lambda: False)
    cfg = {"plex_enabled": True, "jellyfin_enabled": True}
    keys = [e.KEY for e in providers.enabled_engines(cfg)]
    assert keys == ["plex"]  # jellyfin flag on but not configured
