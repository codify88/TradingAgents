"""Price and trend computations for the equity_momentum mandate.

Every figure a Momentum Analyst cites is produced here, in pandas, rather than
recalled by the model: trailing returns, relative strength against a benchmark
and a sector, trend structure, volume confirmation, and the invalidation level
the mandate insists every momentum thesis must name.

The windows are trading days, matching the horizons a mandate declares.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from .quality import Screen, _status

# Trading days per lookback. 21 is the conventional month.
WINDOWS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}

# Used when the sector is unknown: comparing against the index twice is
# honest (and visibly redundant), where guessing a sector is not.
SECTOR_BENCHMARK_DEFAULT = "SPY"

# Sector ETFs, the cheapest honest peer group: a name is judged against what it
# competes with, not only against the index. Keys are Alpha Vantage's Sector
# strings, which are upper case.
SECTOR_ETFS = {
    "TECHNOLOGY": "XLK",
    "INFORMATION TECHNOLOGY": "XLK",
    "FINANCE": "XLF",
    "FINANCIALS": "XLF",
    "LIFE SCIENCES": "XLV",
    "HEALTH CARE": "XLV",
    "HEALTHCARE": "XLV",
    "ENERGY & TRANSPORTATION": "XLE",
    "ENERGY": "XLE",
    "MANUFACTURING": "XLI",
    "INDUSTRIALS": "XLI",
    "TRADE & SERVICES": "XLY",
    "CONSUMER DISCRETIONARY": "XLY",
    "CONSUMER STAPLES": "XLP",
    "REAL ESTATE & CONSTRUCTION": "XLRE",
    "REAL ESTATE": "XLRE",
    "UTILITIES": "XLU",
    "MATERIALS": "XLB",
    "COMMUNICATION SERVICES": "XLC",
}

# A thesis needs a level close enough that being wrong is affordable. Beyond
# this many ATRs the stop is not protection, it is a formality.
MAX_STOP_ATR = 4.0
# Below this, an "invalidation level" is inside the noise and would fire on a
# normal day's range.
MIN_STOP_ATR = 0.8


def _ret(prices: pd.Series, days: int) -> float:
    """Total return over the last ``days`` trading bars, NaN if unavailable."""
    if len(prices) <= days:
        return float("nan")
    start, end = float(prices.iloc[-days - 1]), float(prices.iloc[-1])
    return (end - start) / start if start else float("nan")


def trailing_returns(prices: pd.Series) -> dict[str, float]:
    return {label: _ret(prices, days) for label, days in WINDOWS.items()}


def momentum_12_1(prices: pd.Series) -> float:
    """The classic 12-1 factor: twelve months excluding the most recent one.

    The skipped month is not a detail. Short-horizon reversal runs the other way
    to momentum, so including it dilutes the very signal being measured.
    """
    if len(prices) <= 252:
        return float("nan")
    start, end = float(prices.iloc[-253]), float(prices.iloc[-22])
    return (end - start) / start if start else float("nan")


def excess_returns(stock: pd.Series, other: pd.Series) -> dict[str, float]:
    """Stock return minus ``other``'s over each window, on aligned dates.

    Aligning first matters: a holiday one venue observes and the other does not
    would otherwise shift the windows against each other.
    """
    joined = pd.concat([stock, other], axis=1, join="inner").dropna()
    if joined.empty:
        return {label: float("nan") for label in WINDOWS}
    a, b = joined.iloc[:, 0], joined.iloc[:, 1]
    return {label: _ret(a, days) - _ret(b, days) for label, days in WINDOWS.items()}


def atr(frame: pd.DataFrame, period: int = 14) -> float:
    """Average true range, the unit a stop distance should be measured in."""
    if frame.empty or not {"High", "Low", "Close"} <= set(frame.columns):
        return float("nan")
    high, low, close = frame["High"], frame["Low"], frame["Close"]
    prev = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev).abs(), (low - prev).abs()], axis=1
    ).max(axis=1)
    if len(true_range.dropna()) < period:
        return float("nan")
    return float(true_range.rolling(period).mean().iloc[-1])


def annualized_volatility(prices: pd.Series, days: int = 63) -> float:
    returns = prices.pct_change().dropna().iloc[-days:]
    if len(returns) < 20:
        return float("nan")
    return float(returns.std() * math.sqrt(252))


@dataclass(frozen=True)
class TrendStructure:
    """Where price sits relative to its own recent structure."""

    price: float
    sma50: float
    sma200: float
    sma50_rising: bool | None
    sma200_rising: bool | None
    high_52w: float
    low_52w: float
    pct_from_high: float          # negative = below the high
    pct_above_low: float
    atr14: float
    volatility: float
    up_down_volume: float         # 50-day up-day volume / down-day volume
    recent_vs_avg_volume: float   # last 10 days vs the prior 50
    swing_low: float              # last meaningful higher low, the natural stop


def _slope_rising(series: pd.Series, days: int = 21) -> bool | None:
    s = series.dropna()
    if len(s) <= days:
        return None
    return bool(float(s.iloc[-1]) > float(s.iloc[-days - 1]))


def _swing_low(frame: pd.DataFrame, lookback: int = 63, window: int = 5) -> float:
    """The most recent local low: where the trend would be seen to break.

    A local low is a bar whose low is the lowest in a window centred on it. The
    latest one is the level a trend-follower is actually watching, which is what
    the mandate means by a named invalidation level.
    """
    if frame.empty or "Low" not in frame:
        return float("nan")
    lows = frame["Low"].iloc[-lookback:]
    if len(lows) < 2 * window + 1:
        return float("nan")
    for i in range(len(lows) - window - 1, window - 1, -1):
        centred = lows.iloc[i - window:i + window + 1]
        if float(lows.iloc[i]) == float(centred.min()):
            return float(lows.iloc[i])
    return float(lows.min())


def trend_structure(frame: pd.DataFrame) -> TrendStructure:
    close = frame["Close"]
    price = float(close.iloc[-1])
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    year = frame.iloc[-252:] if len(frame) >= 252 else frame
    high_52w = float(year["High"].max()) if "High" in year else float(year["Close"].max())
    low_52w = float(year["Low"].min()) if "Low" in year else float(year["Close"].min())

    if "Volume" in frame and len(frame) >= 60:
        recent = frame.iloc[-50:]
        change = recent["Close"].diff()
        up = float(recent["Volume"][change > 0].sum())
        down = float(recent["Volume"][change < 0].sum())
        up_down = up / down if down else float("nan")
        last10 = float(frame["Volume"].iloc[-10:].mean())
        prior50 = float(frame["Volume"].iloc[-60:-10].mean())
        recent_vs_avg = last10 / prior50 if prior50 else float("nan")
    else:
        up_down = recent_vs_avg = float("nan")

    return TrendStructure(
        price=price,
        sma50=float(sma50.iloc[-1]) if not math.isnan(sma50.iloc[-1]) else float("nan"),
        sma200=float(sma200.iloc[-1]) if not math.isnan(sma200.iloc[-1]) else float("nan"),
        sma50_rising=_slope_rising(sma50),
        sma200_rising=_slope_rising(sma200),
        high_52w=high_52w,
        low_52w=low_52w,
        pct_from_high=(price - high_52w) / high_52w if high_52w else float("nan"),
        pct_above_low=(price - low_52w) / low_52w if low_52w else float("nan"),
        atr14=atr(frame),
        volatility=annualized_volatility(close),
        up_down_volume=up_down,
        recent_vs_avg_volume=recent_vs_avg,
        swing_low=_swing_low(frame),
    )


def invalidation_levels(t: TrendStructure) -> list[tuple[str, float, float]]:
    """Candidate stop levels as (name, price, distance in ATRs), nearest first.

    Offering several and their ATR distance is the point: the mandate requires a
    level close enough to make the risk tolerable, and that is a question about
    distance, which only ATR answers.
    """
    candidates = [("Last swing low", t.swing_low), ("50-day SMA", t.sma50), ("200-day SMA", t.sma200)]
    out = []
    for name, level in candidates:
        if level is None or math.isnan(level) or level >= t.price:
            continue
        atrs = (t.price - level) / t.atr14 if t.atr14 and not math.isnan(t.atr14) else float("nan")
        out.append((name, float(level), float(atrs)))
    return sorted(out, key=lambda row: -row[1])


def momentum_screens(
    t: TrendStructure, trailing: dict[str, float], vs_bench: dict[str, float],
) -> list[Screen]:
    """The equity_momentum disqualifiers that price alone can settle.

    The fundamental ones (divergence, decelerating growth against an expanding
    multiple) need the growth tools and are screened there.
    """
    screens: list[Screen] = []

    r6, r12 = trailing.get("6m"), trailing.get("12m")
    above50 = None if math.isnan(t.sma50) else t.price > t.sma50
    broken = None
    if above50 is not None and t.sma50_rising is not None:
        broken = (not above50) and (not t.sma50_rising)
    screens.append(Screen(
        "Trend broken: price below a falling 50-day average",
        _status(broken),
        f"price {t.price:.2f} vs 50-day SMA "
        f"{'n/a' if math.isnan(t.sma50) else f'{t.sma50:.2f}'} "
        f"({'rising' if t.sma50_rising else 'falling' if t.sma50_rising is not None else 'n/a'}); "
        f"6m {_pctf(r6)}, 12m {_pctf(r12)}",
    ))

    levels = invalidation_levels(t)
    if not levels:
        status, evidence = "TRIPPED", "no level below the current price among swing low / 50-day / 200-day"
    else:
        name, level, atrs = levels[0]
        if math.isnan(atrs):
            status, evidence = "NO DATA", f"nearest level {name} at {level:.2f}; ATR unavailable"
        elif atrs > MAX_STOP_ATR:
            status = "TRIPPED"
            evidence = f"nearest level {name} at {level:.2f} is {atrs:.1f} ATRs away (limit {MAX_STOP_ATR})"
        elif atrs < MIN_STOP_ATR:
            status = "WATCH"
            evidence = (
                f"nearest level {name} at {level:.2f} is only {atrs:.1f} ATRs away "
                f"(under {MIN_STOP_ATR}); inside normal daily noise and likely to fire early"
            )
        else:
            status = "CLEAR"
            evidence = f"nearest level {name} at {level:.2f}, {atrs:.1f} ATRs away"
    screens.append(Screen("No defensible invalidation level", status, evidence))

    # Leadership is the premise of the strategy: a name that trails its own
    # benchmark over the holding window is not a momentum candidate, whatever
    # its absolute return.
    lagging = None
    b6, b12 = vs_bench.get("6m"), vs_bench.get("12m")
    if not (b6 is None or math.isnan(b6)) and not (b12 is None or math.isnan(b12)):
        lagging = b6 < 0 and b12 < 0
    screens.append(Screen(
        "Lags its benchmark over both the 6- and 12-month windows",
        _status(lagging),
        f"excess vs benchmark: 6m {_pctf(b6)}, 12m {_pctf(b12)}",
    ))
    return screens


def _pctf(x) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.1f}%"
