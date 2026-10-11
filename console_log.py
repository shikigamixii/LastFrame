"""In-memory log buffer behind the Console page.

Keeps the last MAX_ENTRIES log records so the admin can browse them in the
app. Nothing extra is written to disk and the buffer empties on restart;
stdout and audit.log are unchanged.

Two levels exist only here, logged through `logger` (which does not
propagate, so they never reach stdout/audit.log):
    VERBOSE - every HTTP request the app serves
    DEBUG   - [PERF] timings and each Plex/Jellyfin call with its duration
INFO/WARNING/ERROR come from the app's normal loggers via the root handler.
"""
import collections
import logging
import threading
from datetime import datetime, timezone

VERBOSE = 5
logging.addLevelName(VERBOSE, "VERBOSE")

MAX_ENTRIES = 2000


def _level_name(levelno):
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warning"
    if levelno >= logging.INFO:
        return "info"
    if levelno >= logging.DEBUG:
        return "debug"
    return "verbose"


class BufferHandler(logging.Handler):
    """Ring buffer of formatted records, each with an increasing id so the
    page can ask for only what's new since its last poll."""

    def __init__(self, maxlen=MAX_ENTRIES):
        super().__init__(level=VERBOSE)
        self._buf = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, record):
        try:
            msg = record.getMessage()
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                msg += "\n" + record.exc_text
            entry = {
                "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": _level_name(record.levelno),
                "source": record.name,
                "message": msg,
            }
            with self._lock:
                self._seq += 1
                entry["id"] = self._seq
                self._buf.append(entry)
        except Exception:
            self.handleError(record)

    def entries(self, after=0, limit=MAX_ENTRIES):
        """Entries with id > after, oldest first, at most `limit` (newest kept)."""
        with self._lock:
            out = [e for e in self._buf if e["id"] > after]
            last_id = self._seq
        return out[-limit:] if limit else out, last_id


handler = BufferHandler()

logger = logging.getLogger("lastframe")
logger.setLevel(VERBOSE)
logger.propagate = False
logger.addHandler(handler)
