"""Would a different exit have beaten holding to the horizon?

Adapted from LLMQuant's take-profit-lab (MIT; see NOTICE): for every settled
Buy or Overweight, replay the price path from the analysis date to the end of
its holding horizon under each exit rule, and compare the returns. No model
calls; prices are the same dividend-adjusted closes the grader uses.

The rules, each applied to the same entry:

- hold: the return at the horizon -- what the decision was graded on;
- trailing stop 15% / 25%: out when the close falls that far below its peak
  since entry, then in cash (zero return) to the horizon;
- tiered: a third sold at +25%, a third at +50%, the rest held;
- volatility stop: out when the close falls three months' worth of the stock's
  own daily volatility (x3) below its peak.

The "rollercoaster rate" is the share of entries that reached a gain of at
least 30% and then gave back at least half of it by the horizon.

The sample is small, and one window: the study says when it is too small to
prefer any rule rather than naming a winner from noise.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

MIN_SAMPLE = 20
ROLLERCOASTER_PEAK = 0.30
ROLLERCOASTER_GIVEBACK = 0.5
BULLISH = ("Buy", "Overweight")


def _hold(path: pd.Series) -> float:
    return float(path.iloc[-1] / path.iloc[0] - 1)


def _trailing(pct: float) -> Callable[[pd.Series], float]:
    def rule(path: pd.Series) -> float:
        peak = path.cummax()
        hit = path[path <= peak * (1 - pct)]
        exit_price = hit.iloc[0] if not hit.empty else path.iloc[-1]
        return float(exit_price / path.iloc[0] - 1)
    rule.__name__ = f"trailing {pct:.0%}"
    return rule


def _tiered(path: pd.Series) -> float:
    entry = path.iloc[0]
    proceeds, held = 0.0, 1.0
    for level in (1.25, 1.50):
        if held > 1 / 3 + 1e-9 and (path >= entry * level).any():
            proceeds += (1 / 3) * level
            held -= 1 / 3
    return float(proceeds + held * path.iloc[-1] / entry - 1)


def _vol_stop(path: pd.Series, history: pd.Series) -> float:
    daily = history.pct_change().dropna().tail(63)
    if len(daily) < 20:
        return _hold(path)
    width = min(0.6, 3 * float(daily.std()) * math.sqrt(63))
    return _trailing(width)(path)


RULES = ("hold", "trailing 15%", "trailing 25%", "tiered", "volatility stop")


@dataclass
class Replay:
    ticker: str
    date: str
    mandate: str
    returns: dict[str, float]
    peak_gain: float

    @property
    def rollercoaster(self) -> bool:
        final = self.returns["hold"]
        return self.peak_gain >= ROLLERCOASTER_PEAK and (self.peak_gain - final) >= ROLLERCOASTER_GIVEBACK * self.peak_gain


def replay(ticker: str, date: str, holding_days: int, mandate: str,
           prices: Callable[[str], pd.Series]) -> Replay | None:
    close = prices(ticker)
    if close is None or close.empty:
        return None
    start = close.index.searchsorted(pd.Timestamp(date))
    end = start + holding_days
    if start >= len(close) or end >= len(close):
        return None
    path = close.iloc[start:end + 1]
    history = close.iloc[max(0, start - 70):start + 1]
    returns = {"hold": _hold(path), "trailing 15%": _trailing(0.15)(path),
               "trailing 25%": _trailing(0.25)(path), "tiered": _tiered(path),
               "volatility stop": _vol_stop(path, history)}
    return Replay(ticker, date, mandate, returns, float(path.max() / path.iloc[0] - 1))


def _default_prices(ticker: str) -> pd.Series:
    from tradingagents.mandates.tools.financials import alpha_vantage_daily_strict

    frame = alpha_vantage_daily_strict(ticker)
    return frame["Close"] if not frame.empty else pd.Series(dtype=float)


def settled_bullish(entries: list[dict]) -> list[dict]:
    out, seen = [], set()
    for e in entries:
        key = (e["ticker"], e["date"], e.get("mandate") or "")
        holding = str(e.get("holding") or "").rstrip("d")
        if (e.get("rating") in BULLISH and not e.get("pending") and not e.get("superseded")
                and holding.isdigit() and key not in seen):
            seen.add(key)
            out.append(e)
    return out


def study(entries: list[dict], prices: Callable[[str], pd.Series] = _default_prices) -> list[Replay]:
    out = []
    for e in settled_bullish(entries):
        r = replay(e["ticker"], e["date"], int(str(e["holding"]).rstrip("d")), e.get("mandate") or "", prices)
        if r is not None:
            out.append(r)
    return out


def render(replays: list[Replay]) -> str:
    if not replays:
        return "No settled Buy or Overweight decisions with a complete price path yet."
    lines = ["# Exit rules vs holding to the horizon", ""]
    for mandate in sorted({r.mandate for r in replays}):
        group = [r for r in replays if r.mandate == mandate]
        n = len(group)
        coaster = sum(r.rollercoaster for r in group)
        lines += [f"## {mandate or 'no mandate'} ({n} settled bullish call{'s' if n != 1 else ''})", "",
                  "| Rule | Mean return | Median | Worst | Beat hold |", "|---|---|---|---|---|"]
        for rule in RULES:
            vals = sorted(r.returns[rule] for r in group)
            beat = sum(r.returns[rule] > r.returns["hold"] + 1e-9 for r in group)
            lines.append(f"| {rule} | {sum(vals) / n:+.1%} | {vals[n // 2]:+.1%} | {vals[0]:+.1%} | "
                         f"{'—' if rule == 'hold' else f'{beat}/{n}'} |")
        lines += ["", f"Rollercoaster rate: {coaster}/{n} reached +{ROLLERCOASTER_PEAK:.0%} and gave back "
                  f"at least half of it by the horizon.", ""]
        if n < MIN_SAMPLE:
            lines.append(f"Too few settled calls ({n}, fewer than {MIN_SAMPLE}) to prefer any rule: "
                         "read this as a description of these entries, not a recommendation.")
        lines.append("")
    return "\n".join(lines).rstrip()
