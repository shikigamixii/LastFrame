"""Auto-delete orchestration across enabled providers.

The provider-agnostic gating lives in auto_delete_core; each engine implements
the provider-specific sweep/webhook/diagnose logic. This module drives the
periodic sweep and fans it out to every enabled provider. Both the webhook
handlers and the manual sweep endpoint reach the same per-engine logic.
"""
import logging
import threading

from config_store import load_config
import providers as _providers

# Re-exported so callers keep a single import site for the shared gating helpers.
from auto_delete_core import (  # noqa: F401
    is_auto_delete_active, get_enabled_since, candidate_ok,
)

logger = logging.getLogger(__name__)


def _run_sweep(cfg):
    """Run one auto-delete sweep across every enabled provider."""
    total = 0
    for engine in _providers.enabled_engines(cfg):
        try:
            total += engine.run_sweep(cfg) or 0
        except Exception as e:
            logger.warning(f"Auto-delete sweep error ({engine.KEY}): {e}")
    return total


def _auto_delete_sweep():
    """Background timer entry point (rearms itself every 30 minutes)."""
    try:
        cfg = load_config()
        if cfg.get("auto_delete_enabled"):
            _run_sweep(cfg)
    except Exception as e:
        logger.warning(f"Auto-delete sweep error: {e}")
    finally:
        t = threading.Timer(1800, _auto_delete_sweep)
        t.daemon = True
        t.start()
