"""Point-in-time financial statements from Alpha Vantage, shaped for analysis.

Upstream filters statements on ``fiscalDateEnding <= curr_date``. That still
leaks: a fiscal year ending 31 Dec is not public until its earnings release in
February, so a backtest dated 15 Jan would read numbers nobody had yet. Here a
period is admitted only once it was actually *reported* -- the ``reportedDate``
Alpha Vantage's EARNINGS endpoint carries per quarter -- falling back to the
SEC filing deadline when that date is missing. Longer horizons make look-ahead
easier, not harder (docs/design/mandates.md, rule 3).
"""

from __future__ import annotations

import contextlib
import functools
import gzip
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from tradingagents.dataflows.alpha_vantage_common import _make_api_request

logger = logging.getLogger(__name__)

STATEMENTS = ("INCOME_STATEMENT", "BALANCE_SHEET", "CASH_FLOW")

# SEC deadlines for the slowest (non-accelerated) filers: 10-K within 90 days
# of year end, 10-Q within 45. Only used when EARNINGS has no reportedDate for
# a period, so the fallback errs late -- a period admitted too late costs a
# little timeliness; one admitted too early is look-ahead.
ANNUAL_FILING_LAG_DAYS = 90
QUARTERLY_FILING_LAG_DAYS = 45


class FinancialsUnavailable(RuntimeError):
    """No usable statements for this ticker as of this date."""


@functools.lru_cache(maxsize=128)
def _fetch(function: str, symbol: str) -> str:
    """One Alpha Vantage payload, cached for the life of the process.

    Both value analysts read the same three statements; without the cache a run
    would pay for each of them twice. Failures are not cached (lru_cache does
    not memoise exceptions), so a rate-limited call is retried next time.
    """
    return _make_api_request(function, {"symbol": symbol})


def _to_float(value) -> float:
    """Alpha Vantage sends numbers as strings and gaps as the string 'None'."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _frame(reports: list[dict]) -> pd.DataFrame:
    """Reports -> numeric DataFrame indexed by period end, oldest first."""
    if not reports:
        return pd.DataFrame()
    rows = {}
    for r in reports:
        end = r.get("fiscalDateEnding")
        if not end:
            continue
        rows[pd.Timestamp(end)] = {
            k: _to_float(v) for k, v in r.items()
            if k not in ("fiscalDateEnding", "reportedCurrency")
        }
    return pd.DataFrame.from_dict(rows, orient="index").sort_index()


def _merge(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Join statements on period end; the first statement wins a shared column.

    ``netIncome`` appears on both the income statement and the cash-flow
    statement; the income statement's figure is the one ratios are built on.
    """
    merged = pd.DataFrame()
    for f in frames:
        if f.empty:
            continue
        if merged.empty:
            merged = f.copy()
        else:
            merged = merged.join(f[f.columns.difference(merged.columns)], how="outer")
    return merged.sort_index()


def _payload(function: str, symbol: str) -> dict:
    raw = _fetch(function, symbol)
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise FinancialsUnavailable(
            f"Alpha Vantage returned a non-JSON {function} payload for {symbol}"
        ) from None
    if not isinstance(data, dict):
        raise FinancialsUnavailable(f"unexpected {function} payload for {symbol}")
    if "Error Message" in data:
        raise FinancialsUnavailable(f"{function} for {symbol}: {data['Error Message']}")
    return data


@dataclass(frozen=True)
class Financials:
    """Statements for one company, restricted to what was public on ``as_of``."""

    ticker: str
    as_of: pd.Timestamp
    currency: str
    annual: pd.DataFrame      # one row per fiscal year, oldest first
    quarterly: pd.DataFrame   # one row per fiscal quarter, oldest first

    def col(self, name: str, fallbacks: tuple[str, ...] = (), quarterly: bool = False) -> pd.Series:
        """A column, filled from fallbacks where the primary field is missing.

        Alpha Vantage populates fields unevenly across companies (total debt
        sits in ``shortLongTermDebtTotal`` for some filers and only in its
        components for others), so every metric names its fallbacks.
        """
        df = self.quarterly if quarterly else self.annual
        out = df[name] if name in df else pd.Series(float("nan"), index=df.index)
        for fb in fallbacks:
            if fb in df:
                out = out.fillna(df[fb])
        return out.astype(float)

    def ttm(self, name: str, fallbacks: tuple[str, ...] = ()) -> float:
        """Trailing-twelve-month sum of a flow over the last four known quarters.

        NaN unless four quarters are present and span roughly one year, so a
        gap in the filings produces "no data" rather than a three-quarter sum
        dressed up as a year.
        """
        s = self.col(name, fallbacks, quarterly=True).dropna()
        if len(s) < 4:
            return float("nan")
        last4 = s.iloc[-4:]
        span_days = (last4.index[-1] - last4.index[0]).days
        if not 250 <= span_days <= 300:  # ~273 days between Q1 and Q4 period ends
            return float("nan")
        return float(last4.sum())

    def latest(self, name: str, fallbacks: tuple[str, ...] = ()) -> float:
        """Most recent known balance-sheet value, quarterly first then annual."""
        for quarterly in (True, False):
            s = self.col(name, fallbacks, quarterly=quarterly).dropna()
            if not s.empty:
                return float(s.iloc[-1])
        return float("nan")

    @property
    def latest_period(self) -> pd.Timestamp | None:
        idx = self.quarterly.index if not self.quarterly.empty else self.annual.index
        return idx[-1] if len(idx) else None


def _known_dates(index: pd.DatetimeIndex, reported: dict[str, str], lag_days: int) -> pd.Series:
    """Date each period became public: its earnings release, else the filing deadline."""
    known = []
    for end in index:
        date = reported.get(end.strftime("%Y-%m-%d"))
        known.append(pd.Timestamp(date) if date else end + pd.Timedelta(days=lag_days))
    return pd.Series(known, index=index)


def load_financials(ticker: str, as_of: str) -> Financials:
    """Every statement Alpha Vantage has for ``ticker``, as known on ``as_of``.

    When Alpha Vantage has nothing -- it drops a company's statements once it
    delists -- the filings are read from SEC EDGAR instead (see ``edgar.py``),
    so a historical question about a company that later disappeared gets an
    answer rather than a survivors-only silence. Raises FinancialsUnavailable
    when neither source has anything usable by that date; vendor errors
    (missing key, rate limit) propagate so the caller can report them.
    """
    try:
        return _load_alpha_vantage(ticker, as_of)
    except FinancialsUnavailable as av_gap:
        from .edgar import load_delisted_financials

        try:
            return load_delisted_financials(ticker, as_of)
        except FinancialsUnavailable:
            raise av_gap from None
        except Exception as exc:  # EDGAR down or unreadable: report Alpha Vantage's gap
            logger.info("EDGAR fallback for %s failed: %s", ticker, exc)
            raise av_gap from None


def _load_alpha_vantage(ticker: str, as_of: str) -> Financials:
    """Every statement Alpha Vantage has for ``ticker``, as known on ``as_of``."""
    symbol = ticker.strip().upper()
    cutoff = pd.Timestamp(as_of)

    payloads = {fn: _payload(fn, symbol) for fn in STATEMENTS}
    earnings = _payload("EARNINGS", symbol)
    reported = {
        q["fiscalDateEnding"]: q.get("reportedDate")
        for q in earnings.get("quarterlyEarnings", [])
        if q.get("fiscalDateEnding") and q.get("reportedDate") not in (None, "", "None")
    }

    currency = next(
        (r.get("reportedCurrency") for p in payloads.values()
         for r in p.get("annualReports", [])[:1] if r.get("reportedCurrency")),
        "USD",
    )

    def build(key: str, lag: int) -> pd.DataFrame:
        merged = _merge([_frame(payloads[fn].get(key, [])) for fn in STATEMENTS])
        if merged.empty:
            return merged
        known = _known_dates(merged.index, reported, lag)
        return merged[known <= cutoff]

    annual = build("annualReports", ANNUAL_FILING_LAG_DAYS)
    quarterly = build("quarterlyReports", QUARTERLY_FILING_LAG_DAYS)
    if annual.empty:
        raise FinancialsUnavailable(
            f"no annual statements for {symbol} were public by {cutoff.date()}"
        )
    return Financials(symbol, cutoff, currency, annual, quarterly)


# --- prices -----------------------------------------------------------------


class _NoPriceRows(Exception):
    """Yahoo answered with nothing -- raised so the empty answer is never cached."""


def _price_history(symbol: str, start: str, end: str) -> pd.Series:
    """Split-adjusted closes, Alpha Vantage first, Yahoo second, or an empty series.

    Alpha Vantage answers the same way every time on this key; Yahoo throttles,
    and under a screen's load it did so often enough that two runs of one screen
    disagreed. Yahoo stays as the fallback for anything Alpha Vantage lacks.

    Yahoo answers a throttled request with an empty frame, not an error. Caching
    that for the life of the process turned one throttled call into "no price
    history" for every later use of the ticker -- a sweep's remaining cells, or
    a screen's ordering (the 2023-09-01 value screen lost 11 of 17 names' ordering
    values that way, and those names could then only be controls).
    """
    try:
        frame = alpha_vantage_daily_strict(symbol)
    except Exception:
        frame = pd.DataFrame()
    if not frame.empty:
        lo, hi = pd.Timestamp(start), pd.Timestamp(end)
        close = frame["Split Close"]
        close = close[(close.index >= lo) & (close.index < hi)].dropna()
        if not close.empty:
            return close
    try:
        return _price_history_cached(symbol, start, end)
    except _NoPriceRows:
        return pd.Series(dtype=float)


@functools.lru_cache(maxsize=64)
def _price_history_cached(symbol: str, start: str, end: str) -> pd.Series:
    import yfinance as yf

    from tradingagents.dataflows.symbol_utils import normalize_symbol

    # auto_adjust=False: "Close" is split-adjusted but NOT dividend-adjusted,
    # which is what a market capitalisation needs. Alpha Vantage restates share
    # counts for splits, so the two agree across a split.
    hist = yf.Ticker(normalize_symbol(symbol)).history(
        start=start, end=end, auto_adjust=False,
    )
    if hist is None or hist.empty or "Close" not in hist:
        raise _NoPriceRows(symbol)
    close = hist["Close"].astype(float)
    if getattr(close.index, "tz", None) is not None:
        close.index = close.index.tz_localize(None)
    return close.sort_index()


def price_history(ticker: str, start: pd.Timestamp, as_of: pd.Timestamp) -> pd.Series:
    """Daily closes from ``start`` through ``as_of`` inclusive, never beyond it."""
    end = (as_of + pd.Timedelta(days=1)).strftime("%Y-%m-%d")  # yfinance end is exclusive
    series = _price_history(ticker.strip().upper(), start.strftime("%Y-%m-%d"), end)
    if series.empty:
        # No dates to filter: an empty series has an integer index, and comparing
        # it with a Timestamp raises -- which is how a throttled Yahoo answer used
        # to surface as a TypeError instead of as "no history".
        return series
    return series[series.index <= as_of]


@functools.lru_cache(maxsize=64)
def _ohlcv_history(symbol: str, start: str, end: str) -> pd.DataFrame:
    import yfinance as yf

    from tradingagents.dataflows.symbol_utils import normalize_symbol

    # auto_adjust=True here, unlike _price_history: a return series must be
    # total-return (dividends reinvested) to compare one instrument against
    # another, whereas a market capitalisation must not be dividend-adjusted.
    hist = yf.Ticker(normalize_symbol(symbol)).history(
        start=start, end=end, auto_adjust=True,
    )
    if hist is None or hist.empty or "Close" not in hist:
        return pd.DataFrame()
    frame = hist[[c for c in ("Open", "High", "Low", "Close", "Volume") if c in hist]].astype(float)
    if getattr(frame.index, "tz", None) is not None:
        frame.index = frame.index.tz_localize(None)
    return frame.sort_index()


def ohlcv_history(ticker: str, start: pd.Timestamp, as_of: pd.Timestamp) -> pd.DataFrame:
    """Daily OHLCV through ``as_of`` inclusive, dividend-adjusted, never beyond it.

    Momentum reads volume and highs, which the valuation path never needed.
    Adjusted closes so a high-yield name is not scored as a weaker trend than an
    equivalent one that pays nothing.
    """
    end = (as_of + pd.Timedelta(days=1)).strftime("%Y-%m-%d")  # yfinance end is exclusive
    frame = _ohlcv_history(ticker.strip().upper(), start.strftime("%Y-%m-%d"), end)
    return frame[frame.index <= as_of] if not frame.empty else frame


@functools.lru_cache(maxsize=128)
def overview(ticker: str) -> dict:
    """Alpha Vantage OVERVIEW, for the sector a name should be ranked within.

    Not point-in-time: it describes the company today. Only fields that do not
    move -- sector, industry -- may be read from it on a historical run.
    """
    try:
        data = json.loads(_fetch("OVERVIEW", ticker.strip().upper()))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# Where a day's Alpha Vantage daily histories are kept on disk, or None (off).
# A screen asks for ~5,300 of them, and its reruns and its other dates ask for
# the same ones again; the same day's answer is the same answer. Off by default
# so nothing but a caller that opts in (the screener) writes to disk -- tests
# stub the vendor per symbol and must never read another test's answer back.
_daily_cache_dir: Path | None = None
DAILY_CACHE_KEEP_DAYS = 3
# Waits before re-asking when a daily history comes back as a JSON error body.
DAILY_JSON_RETRY_DELAYS = (2.0, 5.0)


@contextlib.contextmanager
def daily_disk_cache(directory):
    """Keep daily histories under ``directory`` for the duration of the block.

    Restores whatever was set before on exit, so a screen run inside a longer
    process -- or a test -- leaves no process-wide cache switched on behind it.
    """
    global _daily_cache_dir
    previous = _daily_cache_dir
    _daily_cache_dir = Path(directory) / "av_daily" if directory else None
    if _daily_cache_dir is not None:
        _daily_cache_dir.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - DAILY_CACHE_KEEP_DAYS * 86400
        for old in _daily_cache_dir.glob("*.csv.gz"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
    try:
        yield
    finally:
        _daily_cache_dir = previous


def _cached_daily_path(symbol: str) -> Path | None:
    if _daily_cache_dir is None:
        return None
    return _daily_cache_dir / f"{symbol}.csv.gz"


def daily_is_cached(symbol: str) -> bool:
    """Whether today's history for ``symbol`` is already on disk (no request needed)."""
    path = _cached_daily_path(symbol.strip().upper())
    return bool(path and path.exists()
                and pd.Timestamp(path.stat().st_mtime, unit="s").date() == pd.Timestamp.now().date())


def _daily_csv(symbol: str):
    """The TIME_SERIES_DAILY_ADJUSTED body for ``symbol``, from today's disk copy if any."""
    path = _cached_daily_path(symbol)
    if path is not None and daily_is_cached(symbol):
        return gzip.decompress(path.read_bytes()).decode()
    body = _make_api_request(
        "TIME_SERIES_DAILY_ADJUSTED",
        {"symbol": symbol, "outputsize": "full", "datatype": "csv"},
    )
    # A JSON body means "no such symbol" -- except when it does not: Alpha
    # Vantage occasionally answers a valid symbol with {"Error Message":
    # "Invalid API call"} (EPRT, GLBS and VRT once in a 5,250-name screen, all
    # fine seconds later). Believed at once, that is a permanent exclusion for
    # the run; a symbol that is really absent answers the same way every time.
    for delay in DAILY_JSON_RETRY_DELAYS:
        if not (isinstance(body, str) and body.lstrip().startswith("{")):
            break
        time.sleep(delay)
        body = _make_api_request(
            "TIME_SERIES_DAILY_ADJUSTED",
            {"symbol": symbol, "outputsize": "full", "datatype": "csv"},
        )
    if path is not None and isinstance(body, str) and body.startswith("timestamp,"):
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(gzip.compress(body.encode()))
        tmp.replace(path)
    return body


@functools.lru_cache(maxsize=512)
def alpha_vantage_daily_strict(symbol: str) -> pd.DataFrame:
    """As :func:`alpha_vantage_daily`, but a vendor failure raises.

    Only answers are cached -- "this symbol has no history" included. A rate
    limit or network error propagates (lru_cache never memoises an exception),
    so one throttled call cannot become a permanent "no data" for the rest of
    the process. Callers that can retry, or that must tell an outage from a
    dead company, use this.
    """
    import csv
    import io

    body = _daily_csv(symbol.strip().upper())
    if not isinstance(body, str) or body.lstrip().startswith("{"):
        return pd.DataFrame()  # a JSON body here is "no such symbol", an answer
    rows = list(csv.DictReader(io.StringIO(body)))
    if not rows or "adjusted_close" not in rows[0]:
        return pd.DataFrame()
    frame = pd.DataFrame(rows).set_index("timestamp")
    frame.index = pd.to_datetime(frame.index)
    frame = frame.sort_index()
    raw = frame[["open", "high", "low", "close", "adjusted_close", "volume"]].astype(float)
    factor = raw["adjusted_close"] / raw["close"].where(raw["close"] > 0)
    # Split-only adjustment: each close divided by every split that came after
    # it. A market capitalisation needs this -- statements restate share counts
    # for splits but not for dividends -- where a return series needs Close.
    if "split_coefficient" in frame:
        splits = pd.to_numeric(frame["split_coefficient"], errors="coerce").fillna(1.0).replace(0.0, 1.0)
        later = splits[::-1].cumprod()[::-1].shift(-1, fill_value=1.0)
        split_close = raw["close"] / later
    else:
        split_close = raw["close"]
    out = pd.DataFrame({
        "Open": raw["open"] * factor,
        "High": raw["high"] * factor,
        "Low": raw["low"] * factor,
        "Close": raw["adjusted_close"],
        "Volume": raw["volume"],
        # As reported, for callers that quote prices rather than compute
        # returns from them (the verified market snapshot).
        "Raw Open": raw["open"],
        "Raw High": raw["high"],
        "Raw Low": raw["low"],
        "Raw Close": raw["close"],
        "Split Close": split_close,
    })
    return out.dropna(subset=["Close"]).sort_index()


def alpha_vantage_daily(symbol: str) -> pd.DataFrame:
    """Full daily history from Alpha Vantage, shaped like a yfinance auto-adjusted frame.

    The reason it exists: Yahoo drops a ticker's history once it delists, so any
    historical question about a company that no longer trades -- was it in the
    universe then, what did it return, how did the call on it turn out -- gets
    no answer, and the survivors are all that is left to study. Alpha Vantage
    keeps the history (Atlas Air through its 2023 take-private, for one).

    Columns Open/High/Low/Close/Volume, prices dividend- and split-adjusted by the
    ratio of adjusted to raw close, so it is interchangeable with what the
    screener and the grader read from yfinance. Empty on any failure, and a
    failure is not cached, so the next call tries again.
    """
    try:
        return alpha_vantage_daily_strict(symbol)
    except Exception:
        return pd.DataFrame()


def close_on_or_before(prices: pd.Series, when: pd.Timestamp) -> tuple[pd.Timestamp, float] | None:
    """The last close at or before ``when`` (within a week), or None."""
    window = prices[(prices.index <= when) & (prices.index > when - pd.Timedelta(days=7))]
    if window.empty:
        return None
    return window.index[-1], float(window.iloc[-1])
