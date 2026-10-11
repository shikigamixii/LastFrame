"""Tests for the in-memory log buffer behind the Console page."""
import logging

import console_log as cl


def _handler_with_logger(name, maxlen=5):
    h = cl.BufferHandler(maxlen=maxlen)
    lg = logging.getLogger(name)
    lg.handlers = [h]
    lg.setLevel(cl.VERBOSE)
    lg.propagate = False
    return h, lg


def test_levels_are_mapped():
    h, lg = _handler_with_logger("t.levels")
    lg.log(cl.VERBOSE, "v")
    lg.debug("d")
    lg.info("i")
    lg.warning("w")
    lg.error("e")
    entries, last_id = h.entries()
    assert [e["level"] for e in entries] == ["verbose", "debug", "info", "warning", "error"]
    assert last_id == 5 and entries[0]["source"] == "t.levels"


def test_ring_buffer_keeps_newest_and_ids_keep_counting():
    h, lg = _handler_with_logger("t.ring", maxlen=3)
    for i in range(5):
        lg.info(f"m{i}")
    entries, last_id = h.entries()
    assert [e["message"] for e in entries] == ["m2", "m3", "m4"]
    assert [e["id"] for e in entries] == [3, 4, 5] and last_id == 5


def test_entries_after_returns_only_new():
    h, lg = _handler_with_logger("t.after")
    lg.info("a")
    lg.info("b")
    entries, last_id = h.entries(after=1)
    assert [e["message"] for e in entries] == ["b"] and last_id == 2
    assert h.entries(after=2)[0] == []


def test_exception_text_is_included():
    h, lg = _handler_with_logger("t.exc")
    try:
        raise ValueError("boom")
    except ValueError:
        lg.exception("failed")
    msg = h.entries()[0][0]["message"]
    assert msg.startswith("failed") and "ValueError: boom" in msg


def test_console_logger_does_not_propagate():
    assert cl.logger.propagate is False
    assert cl.handler in cl.logger.handlers
