"""The LEAPS overlay's LLM tool: the rule's contract, its cost, and its screens.

Mirrors momentum_tools.py: cited markdown over computed figures, and any data
failure an explicit UNAVAILABLE notice rather than a graph crash.
"""

from __future__ import annotations

from typing import Annotated

from langchain_core.tools import tool

from . import leaps as lp
from .render import guarded as _guarded, pct as _pct, screens_block, table as _table

# Momentum first (docs/design/leaps.md, decisions): the call is held for the
# momentum mandate's own horizon.
HORIZON_DAYS = 126
# The agents judge the stock-replacement contract; the at-the-money one is
# priced beside it and graded alongside, never chosen by the agents.
TARGET_DELTA = 0.75
COMPARISON_DELTA = 0.5

_CITE = (
    "Cite these figures as given. Do not recompute them, and do not choose a "
    "different strike or expiry: the contract is set by rule so that every "
    "LEAPS decision can be graded against the same one."
)


def _row(label: str, p, view: lp.LeapsView) -> list[str]:
    if p is None:
        return [label] + ["n/a"] * 10
    c = p.contract
    return [
        label, c.contract_id, str(c.expiry.date()), f"{c.strike:.2f}",
        f"{c.bid:.2f} / {c.ask:.2f}", _pct(c.spread, signed=False), str(c.open_interest),
        f"{_pct(p.iv, signed=False)} / {p.delta:.2f}",
        f"{p.extrinsic:.2f} ({_pct(p.extrinsic / p.underlying, signed=False)} of the stock)",
        _pct(view.breakeven(p)), f"{p.leverage:.1f}x",
    ]


@_guarded
def _leaps_report(ticker: str, curr_date: str) -> str:
    v = lp.leaps_view(ticker, curr_date, HORIZON_DAYS, TARGET_DELTA, COMPARISON_DELTA)
    horizon_years = HORIZON_DAYS / 252
    return "\n\n".join([
        f"## LEAPS candidates for {ticker} as of {curr_date}",
        f"Stock {v.underlying:.2f} (raw close). Risk-free {_pct(v.rate, signed=False)} "
        f"(2-year Treasury). Trailing dividend yield {_pct(v.dividend_yield, signed=False)} -- "
        f"a call holder forgoes about {_pct(v.dividend_yield * horizon_years, signed=False)} "
        f"of the stock price in dividends over the {HORIZON_DAYS}-day hold. "
        f"Realised volatility ({lp.REALISED_VOL_DAYS} days) {_pct(v.realised_vol, signed=False)}.",
        "### The contract (selected by rule)\n"
        f"Shortest expiry at least {HORIZON_DAYS} + 63 trading days out whose strike nearest "
        f"the target delta is liquid. Implied volatility and delta are computed from the "
        f"bid/ask mid, not taken from the vendor.\n"
        + _table(
            ["Role", "Contract", "Expiry", "Strike", "Bid / ask", "Spread", "Open int.",
             "IV / delta", "Time value at the ask", f"Break-even by day {HORIZON_DAYS}",
             "Leverage"],
            [_row(f"Judged (delta {TARGET_DELTA})", v.contract, v),
             _row(f"Comparison (delta {COMPARISON_DELTA}, graded alongside)", v.comparison, v)],
        )
        + "\nBreak-even is the stock move by the exit date at which the call, bought at the "
          "ask and sold at the bid, returns its cost. Leverage is the call's percent move "
          "per 1% move in the stock.",
        screens_block(lp.leaps_screens(v), "LEAPS"),
        _CITE,
    ])


@tool
def get_leaps_candidates(
    ticker: Annotated[str, "ticker symbol"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
) -> str:
    """The long-dated call the LEAPS rule selects for this stock, priced from
    that day's option chain: its cost, time value, break-even move by the
    exit, leverage, an at-the-money comparison, and the LEAPS screens."""
    return _leaps_report(ticker, curr_date)


LEAPS_TOOLS = (get_leaps_candidates,)
