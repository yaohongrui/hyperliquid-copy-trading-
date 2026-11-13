"""Count outbound Hyperliquid requests and log the totals once a minute.

Logging only: ``record`` never blocks the caller, retries, or changes the
request flow.  It exists so the request budget (see the polling changes in
8b510c1 / b98c259) can be observed without adding live metrics plumbing.
"""

import logging
import threading
import time

log = logging.getLogger(__name__)

WINDOW = 60.0
_lock = threading.Lock()
_counts = {}
_started = time.monotonic()


def record(kind):
    """Count one request of ``kind``; log the finished window when due."""
    global _started
    key = str(kind or "other")
    with _lock:
        _counts[key] = _counts.get(key, 0) + 1
        now = time.monotonic()
        if now - _started < WINDOW:
            return
        counts = dict(_counts)
        _counts.clear()
        _started = now
    totals = " ".join(f"{name}={count}" for name, count in sorted(counts.items()))
    log.info("REQUESTS window=%.0fs %s", WINDOW, totals)


def reset():
    """Clear the counters (used by tests)."""
    global _started
    with _lock:
        _counts.clear()
        _started = time.monotonic()
