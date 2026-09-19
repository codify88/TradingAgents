"""The LEAPS decision: is the call a better way to hold the thesis than the stock?

Build step 2 of docs/design/leaps.md. The contract is chosen by rule
(:func:`options.select_call`); this module measures what that contract costs
and screens it. Every figure is computed here, so the LEAPS Analyst judges
the numbers rather than producing them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from . import options as op
from .quality import Screen, _status

# Implied volatility this far above the stock's own trailing realised
# volatility means paying for moves it has not been making.
MAX_IV_TO_REALISED = 1.3
REALISED_VOL_DAYS = 252
# The call is too costly when the stock must beat this many standard deviations
# of move by the exit just to return the premium. One sigma never bit on real
# chains (NVDA 2025-11-25: +9.6% needed against +41.8%). A half-sigma rally
# happens in roughly a third of holds, so needing more than that to break even
# makes the call a long shot. A starting point for the backtest to set, not a
# fitted value.
BREAKEVEN_SIGMAS = 0.5


def realised_vol(close: pd.Series, date: str, days: int = REALISED_VOL_DAYS) -> float:
    """Annualised volatility of daily log returns over ``days`` sessions up to ``date``.

    ``close`` must be split-adjusted: a raw series would read a split as a crash.
    """
    s = close.loc[:pd.Timestamp(date)].dropna()
    if len(s) < days // 2:
        return float("nan")
    r = (s / s.shift(1)).apply(math.log).dropna().iloc[-days:]
    return float(r.std() * math.sqrt(op.TRADING_DAYS_PER_YEAR))


def horizon_breakeven(p: op.PricedContract, horizon_days: int) -> float:
    """The stock move by the horizon at which the call, bought at the ask and
    sold at the bid on the exit date, breaks even -- or NaN.

    Priced at the same implied volatility, with the remaining time to expiry at
    exit, and the spread paid on both sides: the exit value is haircut by the
    entry quote's bid/mid ratio. This, not the move to expiry, is what a
    126-day hold of a longer-dated contract actually needs.
    """
    c = p.contract
    h = horizon_days / op.TRADING_DAYS_PER_YEAR
    remaining = p.years - h
    if remaining <= 0 or math.isnan(p.iv) or not c.two_sided:
        return float("nan")
    haircut = c.bid / c.mid

    def exit_value(s: float) -> float:
        return op.bsm_call(s, c.strike, remaining, p.rate, p.dividend_yield, p.iv) * haircut

    lo, hi = p.underlying * 0.2, p.underlying * 5
    if exit_value(hi) < c.ask:
        return float("nan")
    for _ in range(100):
        mid = (lo + hi) / 2
        if exit_value(mid) < c.ask:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 / p.underlying - 1


def sigma_move(iv: float, horizon_days: int, sigmas: float = 1.0) -> float:
    """The stock move of ``sigmas`` standard deviations over the horizon at this volatility."""
    if math.isnan(iv):
        return float("nan")
    return math.exp(sigmas * iv * math.sqrt(horizon_days / op.TRADING_DAYS_PER_YEAR)) - 1


@dataclass(frozen=True)
class LeapsView:
    """Everything the LEAPS Analyst sees for one ticker and date."""

    underlying: float
    rate: float
    dividend_yield: float
    realised_vol: float
    horizon_days: int
    contract: op.PricedContract | None      # the one the agents judge
    comparison: op.PricedContract | None    # the other delta, graded alongside

    def breakeven(self, p: op.PricedContract | None) -> float:
        return horizon_breakeven(p, self.horizon_days) if p else float("nan")


def leaps_screens(v: LeapsView) -> list[Screen]:
    """The LEAPS disqualifiers, judged on the contract the rule selected."""
    p = v.contract
    if p is None:
        return [Screen(
            "No expiry long enough", "TRIPPED",
            f"no listed call runs {v.horizon_days} + "
            f"{op.EXPIRY_BUFFER_TRADING_DAYS} trading days out; v1 does not roll",
        )]
    c = p.contract
    screens = [Screen("No expiry long enough", "CLEAR", f"selected expiry {c.expiry.date()}")]

    screens.append(Screen(
        "Illiquid", _status(not op.is_liquid(c)),
        f"open interest {c.open_interest} (floor {op.MIN_OPEN_INTEREST}); spread "
        f"{c.spread:.1%} of mid (ceiling {op.MAX_SPREAD:.0%})",
    ))

    ratio = p.iv / v.realised_vol if v.realised_vol and not math.isnan(v.realised_vol) else float("nan")
    screens.append(Screen(
        "Expensive volatility", _status(None if math.isnan(ratio) else ratio > MAX_IV_TO_REALISED),
        f"implied {p.iv:.1%} vs {REALISED_VOL_DAYS}-day realised {v.realised_vol:.1%} "
        f"= {ratio:.2f}x (ceiling {MAX_IV_TO_REALISED}x)",
    ))

    be, bar = v.breakeven(p), sigma_move(p.iv, v.horizon_days, BREAKEVEN_SIGMAS)
    costly = None if math.isnan(be) or math.isnan(bar) else be > bar
    screens.append(Screen(
        "Time value too costly", _status(costly),
        f"break-even by the {v.horizon_days}-day exit needs {be:+.1%} from the stock, "
        f"against a {BREAKEVEN_SIGMAS:g}-sigma move of {bar:+.1%} at the contract's own volatility",
    ))
    return screens


def leaps_view(symbol: str, date: str, horizon_days: int, target_delta: float,
               comparison_delta: float) -> LeapsView:
    from .financials import alpha_vantage_daily_strict

    frame = alpha_vantage_daily_strict(symbol)
    raw = frame["Raw Close"].loc[:pd.Timestamp(date)]
    if raw.empty:
        raise ValueError(f"no price for {symbol} on or before {date}")
    s = float(raw.iloc[-1])
    r = op.risk_free_rate(date)
    q = op.dividend_yield(symbol, date, s)
    chain = op.option_chain(symbol, date)
    return LeapsView(
        underlying=s, rate=r, dividend_yield=q,
        realised_vol=realised_vol(frame["Close"], date),
        horizon_days=horizon_days,
        contract=op.select_call(chain, s, date, r, q, horizon_days, target_delta),
        comparison=op.select_call(chain, s, date, r, q, horizon_days, comparison_delta),
    )
