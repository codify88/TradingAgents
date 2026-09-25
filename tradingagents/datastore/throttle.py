"""A request throttle shared by every process that talks to one vendor.

The nightly run, the harvest and an interactive session can overlap, and the
vendor's limit (150 requests a minute on our Alpha Vantage plan; decided
2026-09-25) is per key, not per process. So the bucket lives in the store's
SQLite file and every acquire is a short write transaction.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

# Burst allowance, as a fraction of a minute's budget. Small, so a burst from one
# process cannot take the whole minute from another.
BURST_FRACTION = 0.1

# Refill arithmetic in floats lands a hair under a whole token (0.99999...), and
# the pause that would cover the gap is too small to move a clock: without a
# tolerance and a floor the loop spins forever.
_EPSILON = 1e-9
_MIN_PAUSE = 0.01


def acquire(conn: sqlite3.Connection, name: str, per_minute: float, *,
            clock: Callable[[], float] = time.time,
            sleep: Callable[[float], None] = time.sleep) -> float:
    """Take one token, waiting if none is available. Returns seconds waited."""
    if per_minute <= 0:
        return 0.0
    rate = per_minute / 60.0
    capacity = max(1.0, per_minute * BURST_FRACTION)
    waited = 0.0
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = clock()
            row = conn.execute("SELECT tokens, updated FROM throttle WHERE name=?", (name,)).fetchone()
            tokens = capacity if row is None else min(capacity, row[0] + (now - row[1]) * rate)
            if tokens >= 1.0 - _EPSILON:
                conn.execute("INSERT OR REPLACE INTO throttle VALUES (?,?,?)", (name, max(0.0, tokens - 1.0), now))
                conn.execute("COMMIT")
                return waited
            conn.execute("INSERT OR REPLACE INTO throttle VALUES (?,?,?)", (name, tokens, now))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        pause = max(_MIN_PAUSE, (1.0 - tokens) / rate)
        sleep(pause)
        waited += pause
