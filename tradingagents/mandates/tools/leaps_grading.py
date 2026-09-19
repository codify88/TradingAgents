"""LEAPS build step 4: grade the instrument choice, after the fact.

The backtest already grades each decision's *direction* on the stock. This
grades the *instrument*: for every Buy or Overweight cell, what would the
rule's call have returned over the same horizon, and did holding it beat
holding the shares? It reads the backtest log and the option chains; it
changes nothing the equity grading does.

Every cell is graded two ways against the stock:

- against the shares outright (the call's return vs the stock's total return),
  which mostly measures leverage; and
- against a matched position -- ``delta`` shares per call, the stock exposure
  the call carried on day one -- which is the fair test of whether the
  instrument added anything beyond leverage: time value, the spread and the
  dividends forgone are all paid against it.

Calls the agents chose are compared with the calls they passed on (Buy or
Overweight held as stock), which is the check on their instrument judgement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from . import leaps as lp, options as op

POSITION_RATINGS = ("Buy", "Overweight")
# How many trading days back to look for the contract's exit quote when the
# exit day's chain lacks it (a holiday gap, a missing print).
EXIT_QUOTE_LOOKBACK_DAYS = 5


@dataclass(frozen=True)
class OptionOutcome:
    contract_id: str
    target_delta: float
    delta: float
    entry_ask: float
    exit_value: float            # bid on the exit day, or intrinsic at expiry
    exit_date: pd.Timestamp
    underlying_entry: float      # raw close on the analysis date
    stock_return: float          # total return of the shares over the same window

    @property
    def option_return(self) -> float:
        return self.exit_value / self.entry_ask - 1

    @property
    def edge_vs_matched(self) -> float:
        """Call P&L minus the P&L of ``delta`` shares, as a fraction of the stock price.

        Positive means the call beat the stock exposure it replaced after paying
        its time value, both spreads and the dividends it forwent.
        """
        call_pnl = self.exit_value - self.entry_ask
        matched_pnl = self.delta * self.underlying_entry * self.stock_return
        return (call_pnl - matched_pnl) / self.underlying_entry


def _exit_day(index: pd.DatetimeIndex, date: str, horizon_days: int) -> pd.Timestamp | None:
    """The trading day ``horizon_days`` sessions after the analysis date, or None if not yet reached."""
    start = index.searchsorted(pd.Timestamp(date))
    pos = start + horizon_days
    return index[pos] if pos < len(index) else None


def _exit_value(contract: op.Contract, index: pd.DatetimeIndex, raw_close: pd.Series,
                exit_day: pd.Timestamp, symbol: str) -> tuple[float, pd.Timestamp] | None:
    if exit_day >= contract.expiry:
        # Held to expiry: worth its intrinsic value at the last close on or before it.
        closes = raw_close.loc[:contract.expiry]
        if closes.empty:
            return None
        return max(float(closes.iloc[-1]) - contract.strike, 0.0), contract.expiry
    pos = index.get_loc(exit_day)
    for back in range(EXIT_QUOTE_LOOKBACK_DAYS + 1):
        day = index[pos - back]
        for c in op.option_chain(symbol, day.strftime("%Y-%m-%d")):
            if c.contract_id == contract.contract_id and c.bid > 0:
                return c.bid, day
    return None


def grade(symbol: str, date: str, horizon_days: int, target_delta: float) -> OptionOutcome | None:
    """The rule's call on ``date``, held ``horizon_days`` and sold at the bid; None when ungradable."""
    from .financials import alpha_vantage_daily_strict

    frame = op.with_retry(lambda: alpha_vantage_daily_strict(symbol))
    if frame.empty:
        return None
    index = frame.index
    exit_day = _exit_day(index, date, horizon_days)
    entry = frame["Raw Close"].loc[:pd.Timestamp(date)]
    if exit_day is None or entry.empty:
        return None
    s = float(entry.iloc[-1])
    r = op.risk_free_rate(date)
    q = op.dividend_yield(symbol, date, s)
    picked = op.select_call(op.option_chain(symbol, date), s, date, r, q, horizon_days, target_delta)
    if picked is None:
        return None
    exit_ = _exit_value(picked.contract, index, frame["Raw Close"], exit_day, symbol)
    if exit_ is None:
        return None
    value, when = exit_
    close = frame["Close"]
    entry_adj = float(close.loc[:pd.Timestamp(date)].iloc[-1])
    stock_return = float(close.loc[:when].iloc[-1]) / entry_adj - 1
    return OptionOutcome(
        contract_id=picked.contract.contract_id, target_delta=target_delta, delta=picked.delta,
        entry_ask=picked.contract.ask, exit_value=value, exit_date=when,
        underlying_entry=s, stock_return=stock_return,
    )


@dataclass(frozen=True)
class GradedCell:
    ticker: str
    date: str
    rating: str
    instrument: str | None
    judged: OptionOutcome | None       # the 0.75-delta contract the agents weighed
    comparison: OptionOutcome | None   # the at-the-money contract, graded alongside
    error: str | None = None           # a vendor failure, reported rather than read as "ungradable"


def review(entries, horizon_days: int = 126, deltas: tuple[float, float] = (0.75, 0.5)) -> list[GradedCell]:
    """Grade every Buy/Overweight cell in a backtest log's entries."""
    cells = []
    for e in entries:
        if e.get("rating") not in POSITION_RATINGS:
            continue
        instrument = lp.decision_instrument(e.get("decision", ""))
        try:
            judged = grade(e["ticker"], e["date"], horizon_days, deltas[0])
            comparison = grade(e["ticker"], e["date"], horizon_days, deltas[1])
        except Exception as exc:  # one vendor failure must not end the review
            cells.append(GradedCell(e["ticker"], e["date"], e["rating"], instrument, None, None,
                                    error=f"{type(exc).__name__}: {exc}"))
            continue
        cells.append(GradedCell(e["ticker"], e["date"], e["rating"], instrument, judged, comparison))
    return cells


def _pct(x: float) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.1f}%"


def _mean(xs) -> float:
    xs = [x for x in xs if x is not None and not math.isnan(x)]
    return sum(xs) / len(xs) if xs else float("nan")


def render(cells: list[GradedCell]) -> str:
    if not cells:
        return "No Buy or Overweight cells to grade: only a position can be held through a call."
    rows = []
    for c in cells:
        j, a = c.judged, c.comparison
        rows.append(
            f"| {c.ticker} | {c.date} | {c.rating} | {c.instrument or 'not asked'} | "
            f"{j.contract_id if j else ('vendor error' if c.error else 'ungradable')} | "
            f"{_pct(j.stock_return) if j else 'n/a'} | "
            f"{_pct(j.option_return) if j else 'n/a'} | {_pct(j.edge_vs_matched) if j else 'n/a'} | "
            f"{_pct(a.option_return) if a else 'n/a'} | {_pct(a.edge_vs_matched) if a else 'n/a'} |"
        )
    graded = [c for c in cells if c.judged]
    errors = [c for c in cells if c.error]
    chosen = [c.judged.edge_vs_matched for c in graded if c.instrument == "Call"]
    passed = [c.judged.edge_vs_matched for c in graded if c.instrument == "Stock"]
    lines = [
        "| Ticker | Date | Rating | Instrument | Contract (delta 0.75) | Stock | Call | "
        "Call vs matched shares | ATM call | ATM vs matched |",
        "|---|---|---|---|---|---|---|---|---|---|",
        *rows,
        "",
        f"Graded {len(graded)} of {len(cells)} position cells "
        f"({len(cells) - len(graded)} ungradable: not yet at the horizon, no qualifying "
        f"contract, or no exit quote)"
        + (f"; {len(errors)} of those failed on the vendor and should be rerun." if errors else "."),
        f"- Calls the agents chose: {len(chosen)}, mean edge over matched shares "
        f"{_pct(_mean(chosen))}.",
        f"- Calls they passed on (held as stock): {len(passed)}, mean edge the call "
        f"would have had {_pct(_mean(passed))}.",
        "Edge is the call's P&L minus that of delta shares, as a share of the stock "
        "price: positive means the call beat the stock exposure it replaced after "
        "time value, both spreads and forgone dividends. The instrument judgement "
        "is working when chosen calls out-edge the ones passed on.",
    ]
    return "\n".join(lines)
