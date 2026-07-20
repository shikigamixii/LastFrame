"""Runtime dispatch of item->provider-id lookups by namespaced id prefix.

Shared modules (assignments, recently_added) need to turn a namespaced item id
back into its external provider ids (Tmdb/Imdb/Tvdb), but they must not import
the engines directly — that would create an import cycle. Each engine registers
itself here at import time; the shared modules call get_item_providers().
"""
import idutil

_ENGINES = {}


def register(key, resolver):
    """Register a provider's raw-id -> {Tmdb/Imdb/Tvdb: id} resolver."""
    _ENGINES[key] = resolver


def get_item_providers(item_id):
    """Resolve external provider ids for a namespaced item id.

    Returns {} for unknown/unregistered providers or on any lookup error, so
    callers can treat "no providers" uniformly.
    """
    key, raw = idutil.split_id(item_id)
    resolver = _ENGINES.get(key)
    if resolver is None:
        return {}
    try:
        return resolver(raw) or {}
    except Exception:
        return {}
