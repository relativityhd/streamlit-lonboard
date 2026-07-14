"""Opt-in perf instrumentation shared by serialize.py and component.py.

Set `ST_LONBOARD_PERF=1` to log per-span timings via the
`streamlit_lonboard.perf` logger to stderr, and to have the frontend log its
own parse/build timings to the browser console.

Note: Streamlit's own `--logger.level` only attaches a handler to its own
"streamlit" logger namespace (see streamlit/logger.py), not the real Python
root logger - so without our own handler here, these `.info()` calls would be
silently dropped regardless of Streamlit's log level.

See IMPLEMENTATION_PLAN.md Phase 4.0.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Iterator

_LOGGER = logging.getLogger("streamlit_lonboard.perf")


def perf_enabled() -> bool:
    enabled = os.environ.get("ST_LONBOARD_PERF") == "1"
    if enabled and not _LOGGER.handlers:
        _LOGGER.addHandler(logging.StreamHandler())
        _LOGGER.setLevel(logging.INFO)
        _LOGGER.propagate = False
    return enabled


@contextmanager
def span(name: str) -> Iterator[None]:
    """No-op unless `ST_LONBOARD_PERF=1`; then logs elapsed wall time for `name`."""
    if not perf_enabled():
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        _LOGGER.info("%s: %.2fms", name, elapsed_ms)


def log(message: str, *args: object) -> None:
    """No-op unless `ST_LONBOARD_PERF=1`; then logs `message % args` at info level."""
    if perf_enabled():
        _LOGGER.info(message, *args)
