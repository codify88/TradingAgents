"""Pacing for the fundamentals tier.

Alpha Vantage enforces a per-minute ceiling (measured at ~257/min on the key
this was built against). The fundamentals tier asks for several statements per
name, so eighty names is a few hundred requests -- enough to burst straight
through the window and have most of the tier come back as "unavailable", which
reads in the report as though those companies could not be screened rather than
as though the screener asked too fast.
"""

from __future__ import annotations

import logging
import time
from collections import deque

logger = logging.getLogger(__name__)

# Below the observed ceiling, because the budget is shared with anything else
# using the key and the cost of being slightly slow is seconds.
DEFAULT_REQUESTS_PER_MINUTE = 200


class RateLimiter:
    """Moving-window limiter: never more than ``rate`` requests in any 60s."""

    def __init__(self, rate: int = DEFAULT_REQUESTS_PER_MINUTE, window: float = 60.0):
        self.rate = max(1, rate)
        self.window = window
        self._times: deque[float] = deque()

    def acquire(self, n: int = 1) -> None:
        """Block until ``n`` more requests fit inside the window."""
        for _ in range(n):
            now = time.monotonic()
            while self._times and now - self._times[0] >= self.window:
                self._times.popleft()
            if len(self._times) >= self.rate:
                sleep_for = self.window - (now - self._times[0]) + 0.01
                if sleep_for > 0:
                    logger.debug("rate limit: sleeping %.1fs", sleep_for)
                    time.sleep(sleep_for)
                now = time.monotonic()
                while self._times and now - self._times[0] >= self.window:
                    self._times.popleft()
            self._times.append(time.monotonic())


def with_retry(fn, attempts: int = 3, base_delay: float = 20.0):
    """Run ``fn``, backing off and retrying when the vendor says slow down.

    Only rate-limit errors are retried. A missing filing or an unparseable
    payload is a fact about the company, not a transient condition, and
    retrying it would spend the budget that the throttle exists to protect.
    """
    from tradingagents.dataflows.errors import VendorRateLimitError

    for attempt in range(attempts):
        try:
            return fn()
        except VendorRateLimitError:
            if attempt == attempts - 1:
                raise
            delay = base_delay * (attempt + 1)
            logger.info("rate limited; retrying in %.0fs", delay)
            time.sleep(delay)
    raise RuntimeError("unreachable")
