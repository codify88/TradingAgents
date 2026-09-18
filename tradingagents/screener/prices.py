"""Tier 1: bulk price history, the only market data cheap enough to buy for
thousands of names at once.

yfinance downloads many symbols per request, so the whole universe costs tens of
requests rather than thousands. Everything that can be decided from price and
volume is therefore decided here, before a single per-name API call is spent.
"""

from __future__ import annotations

import contextlib
import io
import logging
import time
from dataclasses import dataclass, field

import pandas as pd

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _quiet():
    """Suppress yfinance's per-symbol delisting chatter.

    A universe screen asks about thousands of symbols and expects many to be
    dead -- that is what the screen is for. yfinance reports each one on stderr
    and through its own logger, which buries the actual report. The counts that
    matter are in the funnel.
    """
    yf_logger = logging.getLogger("yfinance")
    previous = yf_logger.level
    yf_logger.setLevel(logging.CRITICAL)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            yield
    finally:
        yf_logger.setLevel(previous)

# yfinance batches internally, but a very large request is one failure away from
# losing everything; chunking bounds the blast radius and lets partial results
# through.
BATCH_SIZE = 200


@dataclass
class PriceData:
    """Downloaded frames, and the symbols the vendor could not answer for.

    The distinction is the point. A symbol absent because it is dead and a
    symbol absent because the vendor was throttling look identical in a dict of
    frames, and treating the second as the first lets an outage masquerade as
    thousands of companies with no data -- while the screen goes on to emit a
    confident shortlist drawn from whatever leaked through.
    """

    frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    unavailable: set[str] = field(default_factory=set)

    @property
    def failure_rate(self) -> float:
        total = len(self.frames) + len(self.unavailable)
        return len(self.unavailable) / total if total else 0.0


def download(
    symbols: list[str], start: str, end: str, batch_size: int = BATCH_SIZE,
    attempts: int = 3, fallback: frozenset[str] | set[str] = frozenset(),
    limiter=None,
) -> PriceData:
    """OHLCV per symbol, dividend-adjusted, with vendor failures reported.

    A batch that raises, or that comes back empty for every symbol in it, is
    treated as a vendor failure and retried with backoff before its symbols are
    marked unavailable. A batch that returns data for some symbols and not
    others is believed: those others really have no history.

    ``fallback`` names symbols to fetch from Alpha Vantage when Yahoo has
    nothing for them. The caller passes the names that have delisted since the
    screen date: Yahoo drops a ticker's history when it delists, so without
    this every one of them would be excluded as "no price history" and a
    historical screen would see only survivors. It is limited to those names so
    a live screen pays nothing extra for the genuinely dead.
    """
    import yfinance as yf

    data = PriceData()
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        raw = None
        for attempt in range(attempts):
            try:
                with _quiet():
                    raw = yf.download(
                        batch, start=start, end=end, auto_adjust=True, progress=False,
                        group_by="ticker", threads=True,
                    )
            except Exception as exc:
                logger.warning("price batch %d-%d failed: %s", i, i + len(batch), exc)
                raw = None
            if raw is not None and not raw.empty:
                break
            if attempt < attempts - 1:
                delay = 5.0 * (attempt + 1)
                logger.info("empty price batch; retrying in %.0fs", delay)
                time.sleep(delay)

        if raw is None or raw.empty:
            # Every symbol empty is far more likely to be the vendor than a
            # coincidence of two hundred dead listings.
            data.unavailable.update(batch)
            continue

        for symbol in batch:
            try:
                frame = raw[symbol] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            frame = frame.dropna(how="all")
            if frame.empty or "Close" not in frame:
                continue
            if getattr(frame.index, "tz", None) is not None:
                frame.index = frame.index.tz_localize(None)
            data.frames[symbol] = frame.sort_index()

    missing = [s for s in symbols if s in fallback
               and s not in data.frames and s not in data.unavailable]
    if missing:
        from tradingagents.mandates.tools.financials import alpha_vantage_daily_strict

        from .throttle import with_retry

        lo, hi = pd.Timestamp(start), pd.Timestamp(end)
        for symbol in missing:
            if limiter is not None:
                limiter.acquire(1)
            try:
                frame = with_retry(lambda s=symbol: alpha_vantage_daily_strict(s))
            except Exception as exc:
                # Still failing after retries is the vendor, not the company:
                # report it as unavailable rather than as "no price history".
                logger.warning("delisted-name price fallback failed for %s: %s", symbol, exc)
                data.unavailable.add(symbol)
                continue
            frame = frame[(frame.index >= lo) & (frame.index < hi)] if not frame.empty else frame
            if not frame.empty:
                data.frames[symbol] = frame
    return data


def dollar_volume(frame: pd.DataFrame, days: int = 63) -> float:
    """Median daily dollar volume over the recent window.

    The median, not the mean: one halt or one index-rebalance day should not
    make an untradeable name look liquid.
    """
    if frame.empty or "Volume" not in frame:
        return float("nan")
    recent = frame.iloc[-days:]
    return float((recent["Close"] * recent["Volume"]).median())
