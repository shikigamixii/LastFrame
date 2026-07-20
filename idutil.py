"""Provider-namespaced ID helpers.

The dashboard aggregates items and accounts from more than one media server
at once (Plex and/or Jellyfin), so a bare Plex rating key and a bare Jellyfin
item id could collide. Every id that crosses the DB / API / frontend boundary
is therefore prefixed with a short provider tag:

    px_<plex rating key>       e.g. px_12345
    jf_<jellyfin item id>      e.g. jf_9a8b7c6d...

The frontend's sanitizeIdForClient() only allows [A-Za-z0-9_-], so the
separator has to be '_'. Raw Plex ids are numeric and raw Jellyfin ids are
hex/GUID — neither contains '_', so splitting on the first '_' is unambiguous.

Provider *external* ids (Tmdb / Imdb / Tvdb), which are shared across servers
and used to match the same title on both, are deliberately NOT namespaced.
"""

# Ordered so callers that iterate get a stable Plex-then-Jellyfin order.
KEYS = ("plex", "jellyfin")

_PREFIX_BY_KEY = {"plex": "px", "jellyfin": "jf"}
_KEY_BY_PREFIX = {v: k for k, v in _PREFIX_BY_KEY.items()}


def prefix(key):
    """Short tag ('px'/'jf') for a provider key."""
    return _PREFIX_BY_KEY[key]


def make_id(key, raw):
    """Namespace a raw provider id/account id: make_id('plex', '12') -> 'px_12'."""
    return f"{_PREFIX_BY_KEY[key]}_{raw}"


def split_id(nsid):
    """Split a namespaced id into (key, raw).

    Returns (None, original) when the value carries no recognised prefix, so
    callers can treat legacy/bare ids gracefully.
    """
    s = "" if nsid is None else str(nsid)
    if "_" in s:
        pfx, raw = s.split("_", 1)
        key = _KEY_BY_PREFIX.get(pfx)
        if key and raw:
            return key, raw
    return None, s


def key_of(nsid):
    """Provider key for a namespaced id, or None."""
    return split_id(nsid)[0]


def raw_of(nsid):
    """Raw provider id for a namespaced id (or the value unchanged if bare)."""
    return split_id(nsid)[1]
