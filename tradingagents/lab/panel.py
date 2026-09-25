"""The lab's price panel: every name any past universe held, one column each.

Built from the daily histories the store already holds (one Alpha Vantage
request per symbol, full history, delisted names included), so replaying a
screen on hundreds of dates costs no requests. What is missing is fetched once,
within a budget, through the store and its shared throttle.

Four fields, each a date x symbol frame of float32:

- ``open`` and ``close``: dividend- and split-adjusted, for returns;
- ``raw_close``: as traded, for the price floor -- an adjusted 2012 close sits
  far below what the stock traded at once later splits and dividends are
  folded in, and a $5 floor on it would drop names that were never under $5;
- ``volume``: shares as reported, so dollar volume is ``raw_close * volume``.
"""

from __future__ import annotations

import gzip
import json
import logging
import pickle
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from tradingagents.datastore import get_store
from tradingagents.datastore.store import params_key

logger = logging.getLogger(__name__)

VENDOR, ENDPOINT = "alpha_vantage", "TIME_SERIES_DAILY_ADJUSTED"
# A year of history before the first schedule date: the screens need 252 bars.
PANEL_START = "2011-01-01"
SCHEDULE_START = "2012-01-01"
# Always in the panel, whatever the universes hold: the ladder's rungs.
BENCHMARKS = ("SPY", "RSP", "MTUM", "IWD", "IWF", "IWM")
FIELDS = ("open", "close", "raw_close", "volume")


def lab_dir(config: dict) -> Path:
    path = Path(config["data_cache_dir"]) / "lab"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _daily_key(symbol: str) -> str:
    from tradingagents.mandates.tools.financials import _daily_params

    return params_key(_daily_params(symbol))


# --- universes --------------------------------------------------------------------


def listing_dates(start: str = SCHEDULE_START, end: str | None = None) -> list[str]:
    """The first business day of each month: one listing snapshot a month."""
    end = end or pd.Timestamp.now().strftime("%Y-%m-%d")
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end, freq="BMS")]


def universes(config: dict, dates: Iterable[str] | None = None,
              loader: Callable | None = None) -> dict[str, list[str]]:
    """``{listing date: symbols}``, the screener's own tier-0 universe on each date.

    Kept in the lab directory: a past date's listings never change, so each is
    asked for once (one request, stored as final).
    """
    if loader is None:
        from tradingagents.screener.universe import load_universe as loader
    path = lab_dir(config) / "universes.json"
    try:
        known = json.loads(path.read_text())
    except (OSError, ValueError):
        known = {}
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    for d in (listing_dates() if dates is None else dates):
        if d in known or d >= today:
            continue
        known[d] = sorted(c.symbol for c in loader(d))
        path.write_text(json.dumps(known))
    return dict(sorted(known.items()))


# --- histories --------------------------------------------------------------------


def import_legacy(config: dict) -> int:
    """Copy the retired ``av_daily`` files into the store, where they are kept.

    Those files age out after a few days; each is a full history as fetched,
    so it is stored under the same key a request would use, dated by the
    file's own time. A symbol the store already holds is left alone.
    """
    legacy = Path(config["data_cache_dir"]) / "av_daily"
    if not legacy.is_dir():
        return 0
    store = get_store(config)
    n = 0
    for path in sorted(legacy.glob("*.csv.gz")):
        symbol = path.name.removesuffix(".csv.gz")
        key = _daily_key(symbol)
        if store.stored(VENDOR, ENDPOINT, key) is not None:
            continue
        try:
            body = gzip.decompress(path.read_bytes()).decode("utf-8")
        except (OSError, EOFError, UnicodeDecodeError):
            continue
        fetched = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        store.put(VENDOR, ENDPOINT, key, body, symbol=symbol, final=False, fetched_at=fetched)
        n += 1
    return n


def stored_symbols(config: dict) -> set[str]:
    rows = get_store(config)._conn().execute(
        "SELECT symbol FROM response WHERE vendor=? AND endpoint=?", (VENDOR, ENDPOINT)).fetchall()
    return {r[0] for r in rows if r[0]}


def needed_symbols(unis: dict[str, list[str]]) -> set[str]:
    return set().union(*unis.values(), BENCHMARKS) if unis else set(BENCHMARKS)


@dataclass
class FetchReport:
    asked: int = 0
    fetched: int = 0
    empty: int = 0
    failed: int = 0
    stopped: str = ""


def fetch_missing(config: dict, symbols: Iterable[str], max_requests: int,
                  fetch: Callable[[str], pd.DataFrame] | None = None,
                  deadline: datetime | None = None) -> FetchReport:
    """One full history per symbol the store lacks, oldest need first, within a budget.

    An empty answer (no such symbol at the vendor) is still an answer. The
    vendor says so with an error body, which the store never keeps, so the lab
    remembers those symbols itself and does not ask again.
    """
    if fetch is None:
        from tradingagents.mandates.tools.financials import alpha_vantage_daily_strict as fetch
    absent_path = lab_dir(config) / "absent.json"
    try:
        absent = set(json.loads(absent_path.read_text()))
    except (OSError, ValueError):
        absent = set()
    have = stored_symbols(config)
    todo = sorted(set(symbols) - have - absent)
    report = FetchReport()
    consecutive = 0
    for symbol in todo:
        if report.asked >= max_requests:
            report.stopped = f"budget ({max_requests:,} requests)"
            break
        if deadline and datetime.now(UTC) >= deadline:
            report.stopped = "deadline"
            break
        report.asked += 1
        try:
            frame = fetch(symbol)
        except Exception as exc:  # one symbol's failure is not the run's
            logger.warning("history unavailable for %s: %s", symbol, exc)
            report.failed += 1
            consecutive += 1
            if consecutive >= 25:
                report.stopped = "25 vendor failures in a row"
                break
            time.sleep(min(60, 2 * consecutive))
            continue
        consecutive = 0
        if frame is None or frame.empty:
            report.empty += 1
            absent.add(symbol)
            if report.empty % 50 == 1:
                absent_path.write_text(json.dumps(sorted(absent)))
        else:
            report.fetched += 1
    else:
        report.stopped = "done"
    absent_path.write_text(json.dumps(sorted(absent)))
    return report


# --- the panel --------------------------------------------------------------------


@dataclass
class Panel:
    open: pd.DataFrame
    close: pd.DataFrame
    raw_close: pd.DataFrame
    volume: pd.DataFrame
    built: str = ""

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.close.index

    @property
    def symbols(self) -> list[str]:
        return list(self.close.columns)


def _frame_from_body(body: str) -> pd.DataFrame:
    from tradingagents.mandates.tools.financials import parse_daily_body

    return parse_daily_body(body)


def build_panel(config: dict, symbols: Iterable[str], start: str = PANEL_START,
                bodies: Callable[[str], str | None] | None = None) -> Panel:
    """Stack every stored history into one panel. Symbols with nothing stored
    (or only a "no such symbol" answer) are left out, not filled."""
    store = get_store(config)

    def stored(symbol: str) -> str | None:
        row = store.stored(VENDOR, ENDPOINT, _daily_key(symbol))
        return row[0] if row else None

    bodies = bodies or stored
    lo = pd.Timestamp(start)
    cols: dict[str, dict[str, pd.Series]] = {f: {} for f in FIELDS}
    for symbol in sorted(set(symbols)):
        body = bodies(symbol)
        if not body:
            continue
        frame = _frame_from_body(body)
        if frame.empty:
            continue
        frame = frame[frame.index >= lo]
        if frame.empty:
            continue
        frame = frame[~frame.index.duplicated(keep="last")]
        cols["open"][symbol] = frame["Open"]
        cols["close"][symbol] = frame["Close"]
        cols["raw_close"][symbol] = frame["Raw Close"]
        cols["volume"][symbol] = frame["Volume"]
    if not cols["close"]:
        empty = pd.DataFrame(dtype=np.float32)
        return Panel(empty, empty, empty, empty)
    # The benchmark's trading calendar: a date SPY did not trade is not a date.
    spine = cols["close"]["SPY"].index if "SPY" in cols["close"] else None
    out = {}
    for f in FIELDS:
        frame = pd.DataFrame(cols[f]).sort_index()
        if spine is not None:
            frame = frame.reindex(spine)
        out[f] = frame.astype(np.float32)
    return Panel(**out, built=datetime.now(UTC).isoformat(timespec="seconds"))


def save_panel(panel: Panel, config: dict) -> Path:
    path = lab_dir(config) / "panel.pkl"
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as fh:
        pickle.dump(panel, fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)
    return path


def load_panel(config: dict) -> Panel | None:
    path = lab_dir(config) / "panel.pkl"
    try:
        with path.open("rb") as fh:
            return pickle.load(fh)
    except (OSError, pickle.UnpicklingError, EOFError):
        return None
