"""The equity_momentum mandate's LLM tools: cited markdown over computed figures.

Mirrors value_tools.py: the analyst reads these reports and interprets them; it
never recomputes a ratio or supplies one from memory. Any data failure becomes
an explicit UNAVAILABLE notice rather than a graph crash.
"""

from __future__ import annotations

import math
from typing import Annotated

import pandas as pd
from langchain_core.tools import tool

from . import growth as gr, momentum as mo
from .financials import load_financials, ohlcv_history, overview
from .render import (
    flag as _flag,
    guarded as _guarded,
    num as _num,
    pct as _pct,
    screens_block,
    table as _table,
)

_CITE = (
    "Cite these figures as given. Do not recompute them or substitute figures "
    "from memory; where a value is n/a, report the gap."
)


def _series(ticker: str, as_of: pd.Timestamp) -> pd.DataFrame:
    # 13 months of bars for a 12-month window, plus room for the 200-day average.
    start = as_of - pd.Timedelta(days=int(365 * 1.6))
    frame = ohlcv_history(ticker, start, as_of)
    if frame.empty or len(frame) < 30:
        raise RuntimeError(f"no usable price history for {ticker} as of {as_of.date()}")
    return frame


def _sector_etf(ticker: str) -> tuple[str, str]:
    """(sector label, ETF) for the peer comparison, or ('unknown', default)."""
    sector = str(overview(ticker).get("Sector", "")).strip().upper()
    return (sector.title() or "unknown"), mo.SECTOR_ETFS.get(sector, mo.SECTOR_BENCHMARK_DEFAULT)


# --- relative strength ----------------------------------------------------------------


@_guarded
def _relative_strength_report(ticker: str, curr_date: str) -> str:
    as_of = pd.Timestamp(curr_date)
    frame = _series(ticker, as_of)
    close = frame["Close"]

    bench = ohlcv_history("SPY", as_of - pd.Timedelta(days=int(365 * 1.6)), as_of)
    sector_label, etf = _sector_etf(ticker)
    sector = ohlcv_history(etf, as_of - pd.Timedelta(days=int(365 * 1.6)), as_of)

    trailing = mo.trailing_returns(close)
    vs_bench = mo.excess_returns(close, bench["Close"]) if not bench.empty else {}
    vs_sector = mo.excess_returns(close, sector["Close"]) if not sector.empty else {}

    rows = [[
        label,
        _pct(trailing.get(label)),
        _pct(vs_bench.get(label)),
        _pct(vs_sector.get(label)),
    ] for label in mo.WINDOWS]

    return "\n\n".join([
        f"## Relative strength: {ticker.upper()} as of {as_of.date()}",
        f"Benchmark SPY; sector proxy {etf} ({sector_label}).",
        _table(["Window", "Total return", "vs SPY", f"vs {etf}"], rows),
        f"**12-1 momentum** (twelve months excluding the most recent, the classic "
        f"factor definition): {_pct(mo.momentum_12_1(close))}",
        "The excess columns are what this mandate is buying: absolute return that "
        "merely matches the index is not leadership. The 12-1 figure deliberately "
        "skips the last month, where short-horizon reversal runs against momentum.",
        _CITE,
    ])


# --- trend structure ------------------------------------------------------------------


@_guarded
def _trend_structure_report(ticker: str, curr_date: str) -> str:
    as_of = pd.Timestamp(curr_date)
    frame = _series(ticker, as_of)
    t = mo.trend_structure(frame)

    bench = ohlcv_history("SPY", as_of - pd.Timedelta(days=int(365 * 1.6)), as_of)
    trailing = mo.trailing_returns(frame["Close"])
    vs_bench = mo.excess_returns(frame["Close"], bench["Close"]) if not bench.empty else {}

    structure = _table(
        ["Measure", "Value"],
        [
            ["Price", _num(t.price)],
            ["50-day SMA", f"{_num(t.sma50)} ({'rising' if t.sma50_rising else 'falling' if t.sma50_rising is not None else 'n/a'})"],
            ["200-day SMA", f"{_num(t.sma200)} ({'rising' if t.sma200_rising else 'falling' if t.sma200_rising is not None else 'n/a'})"],
            ["Price above 50-day", _flag(None if math.isnan(t.sma50) else t.price > t.sma50)],
            ["Price above 200-day", _flag(None if math.isnan(t.sma200) else t.price > t.sma200)],
            ["52-week high", f"{_num(t.high_52w)} ({_pct(t.pct_from_high)} from price)"],
            ["52-week low", f"{_num(t.low_52w)} ({_pct(t.pct_above_low)} above)"],
            ["ATR(14)", _num(t.atr14)],
            ["Annualized volatility (3m)", _pct(t.volatility, 0, signed=False)],
            ["Up/down volume (50d)", _num(t.up_down_volume)],
            ["Recent vs average volume", _num(t.recent_vs_avg_volume)],
        ],
    )

    levels = mo.invalidation_levels(t)
    if levels:
        level_table = _table(
            ["Candidate level", "Price", "Distance (ATRs)"],
            [[name, _num(level), _num(atrs, 1)] for name, level, atrs in levels],
        )
    else:
        level_table = "No candidate level sits below the current price."

    return "\n\n".join([
        f"## Trend structure: {ticker.upper()} as of {as_of.date()}",
        structure,
        "### Candidate invalidation levels",
        level_table,
        "This mandate requires a named invalidation level. Distance in ATRs is how "
        "to judge one: too far and the stop is a formality rather than protection, "
        "too close and it fires on an ordinary day's range.",
        "### Volume reading",
        "Up/down volume above 1.0 means advances came on heavier trade than "
        "declines, which is accumulation. A 52-week high printed on volume below "
        "the recent average is the opposite, whatever the price did.",
        screens_block(mo.momentum_screens(t, trailing, vs_bench), "equity_momentum"),
        _CITE,
    ])


# --- growth trajectory ----------------------------------------------------------------


def _multiple_expanding(g, price_12m: float) -> tuple[bool | None, float]:
    """Has the multiple expanded over the last year, and by what earnings base?

    Price growth outrunning trailing per-share earnings growth *is* multiple
    expansion -- no forward estimate required, so this works on a historical run
    too. Returns (verdict, the earnings growth it was measured against).
    """
    eps_growth = g.annual_eps_growth.dropna()
    if math.isnan(price_12m) or eps_growth.empty:
        return None, float("nan")
    latest = float(eps_growth.iloc[-1])
    return bool(price_12m > latest), latest


@_guarded
def _growth_trajectory_report(ticker: str, curr_date: str) -> str:
    as_of = pd.Timestamp(curr_date)
    f = load_financials(ticker, curr_date)
    g = gr.growth_trajectory(f)
    revisions = gr.estimate_revisions(ticker, as_of)

    try:
        price_12m = mo.trailing_returns(_series(ticker, as_of)["Close"])["12m"]
    except Exception:
        price_12m = float("nan")  # screens that need it report NO DATA
    multiple_expanding, eps_base = _multiple_expanding(g, price_12m)

    annual = _table(
        ["Fiscal year", "Revenue growth", "EPS growth", "Gross margin", "Operating margin", "Shares out"],
        [[
            str(idx.date()),
            _pct(g.annual_revenue_growth.get(idx)),
            _pct(g.annual_eps_growth.get(idx)),
            _pct(g.gross_margin.get(idx), 1).lstrip("+"),
            _pct(g.operating_margin.get(idx), 1).lstrip("+"),
            "n/a" if math.isnan(g.share_count.get(idx, float("nan"))) else f"{g.share_count[idx] / 1e6:,.0f}M",
        ] for idx in g.annual_revenue_growth.index[-8:]],
    )

    q = g.quarterly_revenue_growth.dropna().iloc[-8:]
    quarterly = _table(
        ["Quarter", "Revenue growth YoY", "EPS growth YoY"],
        [[str(idx.date()), _pct(q.get(idx)), _pct(g.quarterly_eps_growth.get(idx))] for idx in q.index],
    )

    return "\n\n".join([
        f"## Growth trajectory: {ticker.upper()} as of {f.as_of.date()}",
        f"Statements admitted only once reported. Latest period: "
        f"{f.latest_period.date() if f.latest_period is not None else 'n/a'}.",
        "### By fiscal year",
        annual,
        "### By quarter (year-on-year)",
        quarterly,
        _table(
            ["Multiple check", "Value"],
            [
                ["12-month price return", _pct(price_12m)],
                ["Latest annual EPS growth", _pct(eps_base)],
                ["Multiple expanded", _flag(multiple_expanding)],
            ],
        ),
        _table(
            ["Rate of change", "Value"],
            [
                ["Revenue growth acceleration", "n/a" if math.isnan(g.revenue_acceleration) else f"{g.revenue_acceleration:+.1f}pp vs four quarters earlier"],
                ["EPS growth acceleration", "n/a" if math.isnan(g.eps_acceleration) else f"{g.eps_acceleration:+.1f}pp vs four quarters earlier"],
            ],
        ),
        "Acceleration is the figure that matters here, not the level. A high but "
        "decelerating growth rate is the setup that unwinds; read the direction "
        "before the magnitude. EPS is per-share, so buyback-driven growth is not "
        "counted as business growth.",
        screens_block(
            gr.growth_screens(g, revisions, multiple_expanding, price_12m),
            "equity_momentum",
        ),
        _CITE,
    ])


# --- estimate revisions ---------------------------------------------------------------


@_guarded
def _estimate_revisions_report(ticker: str, curr_date: str) -> str:
    as_of = pd.Timestamp(curr_date)
    revisions = gr.estimate_revisions(ticker, as_of)
    if not revisions:
        return (
            f"## Estimate revisions: {ticker.upper()}\n\n"
            "UNAVAILABLE: no forward estimates for this ticker and date. Forward "
            "estimates carry no as-of date, so they are not served on a historical "
            "run -- reading today's consensus into a past date would be look-ahead. "
            "Say that the fundamental-momentum leg is unverified rather than "
            "substituting figures from memory or inferring it from the price."
        )

    rows = [[
        r.period,
        r.horizon,
        "n/a" if math.isnan(r.analysts) else f"{r.analysts:.0f}",
        _num(r.eps_now),
        _pct(r.drift_30d),
        _pct(r.drift_90d),
        f"{r.up_30d:.0f}/{r.down_30d:.0f}" if not math.isnan(r.up_30d) else "n/a",
        "n/a" if math.isnan(r.breadth_30d) else f"{r.breadth_30d:+.2f}",
    ] for r in revisions]

    return "\n\n".join([
        f"## Estimate revisions: {ticker.upper()} as of {as_of.date()}",
        _table(
            ["Period", "Horizon", "Analysts", "EPS consensus", "30d drift", "90d drift", "Up/down (30d)", "Breadth"],
            rows,
        ),
        "These are the consensus as it stands today, not as of the run date: "
        "the vendor stamps no as-of date on an estimate. Over a gap of a few days "
        "that is immaterial against 30- and 90-day revision windows, which is why "
        "it is served at all.",
        "Breadth is (up - down) / total revisions over 30 days, from -1 to +1. "
        "Positive breadth with positive drift is the confirming signal for this "
        "mandate; a turn to negative is the earliest fundamental warning available "
        "and usually precedes the price. Drift is the change in the consensus "
        "itself, which matters more than the count when few analysts cover a name.",
        _CITE,
    ])


# --- tool objects ---------------------------------------------------------------------


@tool
def get_relative_strength(
    ticker: Annotated[str, "ticker symbol"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
) -> str:
    """Trailing 1/3/6/12-month total returns, the excess of each over SPY and
    over the sector ETF, and the 12-1 momentum factor."""
    return _relative_strength_report(ticker, curr_date)


@tool
def get_trend_structure(
    ticker: Annotated[str, "ticker symbol"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
) -> str:
    """Moving-average structure, 52-week high/low position, ATR and volatility,
    volume confirmation, candidate invalidation levels with their ATR distance,
    and the equity_momentum price screens."""
    return _trend_structure_report(ticker, curr_date)


@tool
def get_growth_trajectory(
    ticker: Annotated[str, "ticker symbol"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
) -> str:
    """Revenue and per-share earnings growth by year and by quarter, margin
    direction, share count, and whether the growth rate is accelerating."""
    return _growth_trajectory_report(ticker, curr_date)


@tool
def get_estimate_revisions(
    ticker: Annotated[str, "ticker symbol"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
) -> str:
    """Forward EPS consensus, how far it has drifted over 30 and 90 days, and
    the count and breadth of upward versus downward revisions."""
    return _estimate_revisions_report(ticker, curr_date)


MOMENTUM_TOOLS = (get_relative_strength, get_trend_structure)
GROWTH_TOOLS = (get_growth_trajectory, get_estimate_revisions)
