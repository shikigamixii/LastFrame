"""Provider engine registry and dispatch.

app.py aggregates across every *enabled* provider for list endpoints and
dispatches item-scoped endpoints to the engine that owns the item's namespaced
id. An engine is "enabled" only when its master switch is on AND its credentials
are configured, so users can run Plex and Jellyfin together or toggle either off.
"""
import idutil
from config_store import load_config
import plex_engine
import jellyfin_engine

_ENGINES = {
    plex_engine.KEY: plex_engine,
    jellyfin_engine.KEY: jellyfin_engine,
}


def all_engines():
    """Every known engine, in stable Plex-then-Jellyfin order."""
    return [_ENGINES[k] for k in idutil.KEYS if k in _ENGINES]


def get_engine(key):
    return _ENGINES.get(key)


def enabled_engines(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    return [e for e in all_engines() if e.is_enabled(cfg)]


def configured_engines():
    return [e for e in all_engines() if e.is_configured()]


def engine_for_id(nsid, cfg=None, require_enabled=True):
    """Resolve (engine, raw_id) for a namespaced id.

    Returns (None, raw) when the prefix is unknown or, with require_enabled,
    when that provider is currently disabled.
    """
    key, raw = idutil.split_id(nsid)
    e = _ENGINES.get(key)
    if e is None:
        return None, raw
    if require_enabled:
        cfg2 = cfg if cfg is not None else load_config()
        if not e.is_enabled(cfg2):
            return None, raw
    return e, raw
