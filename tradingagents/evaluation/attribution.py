"""Where a strategy's return comes from: the benchmark ladder, from prices.

Each saved screen is judged on its own mandate's horizon (756 trading days for
value, 126 for momentum, 5 with no mandate) against a ladder of benchmarks, each
rung separating one source of return:

    picks - S&P 500 = (style index - S&P 500)   style: was the style in favour?
                    + (controls - style index)   universe: our eligible pool vs the style
                    + (picks - controls)         selection: the screen's ranking

and, beside it, equal-weight S&P (RSP) minus the S&P 500 -- how much of a gap is
only size and weighting, since the random controls are equal-weighted and lean
smaller than a cap-weighted index. The agent layer is read from the decision
logs: the forward return of names the agents rated Buy/Overweight against those
they did not, on the same screen.

Everything here is computed from dividend-adjusted closes, so it depends on
neither the agents' ratings nor the alpha the log recorded -- it costs no model
calls, and a screen counts once its horizon has traded.

Design: docs/design/implementation-plan.md (tuning wave).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

MARKET, EQUAL_WEIGHT = "SPY", "RSP"
BULLISH = ("Buy", "Overweight")


def _mean(xs: list[float]) -> float:
    xs = [x for x in xs if not math.isnan(x)]
    return sum(xs) / len(xs) if xs else float("nan")


def forward_return(prices: Callable[[str], pd.Series], symbol: str, day: str, horizon: int) -> float:
    close = prices(symbol)
    if close is None or close.empty:
        return float("nan")
    i = close.index.searchsorted(pd.Timestamp(day))
    j = i + horizon
    if i >= len(close) or j >= len(close):
        return float("nan")
    return float(close.iloc[j] / close.iloc[i] - 1)


def _default_prices(symbol: str) -> pd.Series:
    from tradingagents.mandates.tools.financials import alpha_vantage_daily_strict

    try:
        frame = alpha_vantage_daily_strict(symbol)
    except Exception:  # an unreachable vendor leaves the figure blank
        return pd.Series(dtype=float)
    return frame["Close"] if not frame.empty else pd.Series(dtype=float)


@dataclass
class Ladder:
    screen: str
    mandate: str
    as_of: str
    horizon: int
    style_symbol: str
    market: float
    style: float
    equal: float
    picks: float
    controls: float
    n_picks: int
    n_controls: int
    bullish: float
    other: float
    n_bullish: int
    n_other: int

    @property
    def complete(self) -> bool:
        return not any(math.isnan(x) for x in (self.market, self.style, self.picks, self.controls))

    @property
    def style_term(self) -> float:
        return self.style - self.market

    @property
    def universe_term(self) -> float:
        return self.controls - self.style

    @property
    def selection(self) -> float:
        return self.picks - self.controls

    @property
    def size_term(self) -> float:
        return self.equal - self.market

    @property
    def agents(self) -> float:
        return self.bullish - self.other if self.n_bullish and self.n_other else float("nan")


def ladders(config: dict, mandate: str | None = None,
            prices: Callable[[str], pd.Series] = _default_prices) -> list[Ladder]:
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.mandates.registry import get_mandate
    from tradingagents.screener.manifest import load_manifests
    from tradingagents.screener.review import decision_logs

    entries = {(e["ticker"], e["date"], e.get("mandate") or ""): e
               for log in decision_logs(config) for e in log.load_entries() if not e.get("superseded")}
    cache: dict[str, pd.Series] = {}

    def px(symbol: str) -> pd.Series:
        if symbol not in cache:
            cache[symbol] = prices(symbol)
        return cache[symbol]

    out = []
    for m in load_manifests(config, mandate):
        mand = get_mandate(m.mandate) if m.mandate else None
        horizon = mand.horizon_days if mand else TradingAgentsGraph.DEFAULT_HOLDING_DAYS
        style_symbol = (mand.benchmark if mand and mand.benchmark else MARKET)

        def fr(symbol: str, _m=m, _h=horizon) -> float:
            return forward_return(px, symbol, _m.as_of, _h)

        picks = [fr(s) for s in m.pick_symbols]
        controls = [fr(s) for s in m.control_symbols]
        bull, other = [], []
        for s in m.pick_symbols + m.control_symbols:
            e = entries.get((s, m.as_of, m.mandate or ""))
            if e is None:
                continue
            (bull if e.get("rating") in BULLISH else other).append(fr(s))
        out.append(Ladder(
            screen=m.run_id, mandate=m.mandate or "", as_of=m.as_of, horizon=horizon,
            style_symbol=style_symbol, market=fr(MARKET), style=fr(style_symbol), equal=fr(EQUAL_WEIGHT),
            picks=_mean(picks), controls=_mean(controls),
            n_picks=sum(not math.isnan(x) for x in picks), n_controls=sum(not math.isnan(x) for x in controls),
            bullish=_mean(bull), other=_mean(other),
            n_bullish=sum(not math.isnan(x) for x in bull), n_other=sum(not math.isnan(x) for x in other)))
    return out


def _pct(x: float) -> str:
    return "—" if math.isnan(x) else f"{x:+.1%}"


def summary(rows: list[Ladder]) -> dict[str, dict[str, float]]:
    """Per mandate, averages over screens whose ladder is complete."""
    out: dict[str, dict[str, float]] = {}
    for mandate in sorted({r.mandate for r in rows}):
        done = [r for r in rows if r.mandate == mandate and r.complete]
        if not done:
            continue
        with_agents = [r for r in done if not math.isnan(r.agents)]
        out[mandate] = {
            "screens": len(done),
            "picks_vs_market": _mean([r.picks - r.market for r in done]),
            "style": _mean([r.style_term for r in done]),
            "universe": _mean([r.universe_term for r in done]),
            "selection": _mean([r.selection for r in done]),
            "selection_positive": sum(r.selection > 0 for r in done),
            "size": _mean([r.size_term for r in done]),
            "agents": _mean([r.agents for r in with_agents]),
            "agent_screens": len(with_agents),
        }
    return out


def summary_line(mandate: str, s: dict[str, float]) -> str:
    agents = (f"; agents (Buy/OW minus the rest) {_pct(s['agents'])} on {s['agent_screens']} screen(s)"
              if s["agent_screens"] else "; agents: no rated screens yet")
    return (f"{mandate or 'no mandate'} ({s['screens']} screens): picks vs S&P 500 {_pct(s['picks_vs_market'])} "
            f"= style {_pct(s['style'])} + universe {_pct(s['universe'])} + selection {_pct(s['selection'])} "
            f"(positive on {s['selection_positive']}/{s['screens']}); size/equal-weight {_pct(s['size'])}{agents}")


def render(rows: list[Ladder]) -> str:
    if not rows:
        return "No screens yet."
    lines = ["# Attribution: the benchmark ladder, from prices", "",
             "Each screen on its own mandate's horizon. picks - S&P 500 = style (style index - S&P 500) "
             "+ universe (controls - style index) + selection (picks - controls). Size/equal-weight is "
             "RSP - S&P 500. Agents: forward return of names rated Buy/Overweight minus the rest.", "",
             "| Screen | Mandate | As of | Days | S&P 500 | Style | Equal-wt | Controls | Picks | Style term "
             "| Universe | Selection | Agents |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(
            f"| {r.screen.rsplit('_', 1)[-1]} | {(r.mandate or 'none').removeprefix('equity_')} | {r.as_of} "
            f"| {r.horizon} | {_pct(r.market)} | {_pct(r.style)} ({r.style_symbol}) | {_pct(r.equal)} "
            f"| {_pct(r.controls)} | {_pct(r.picks)} | {_pct(r.style_term)} | {_pct(r.universe_term)} "
            f"| {_pct(r.selection)} | {_pct(r.agents)} |")
    lines += ["", "## Summary", ""]
    s = summary(rows)
    lines += [f"- {summary_line(m, v)}" for m, v in s.items()] or ["No screen's horizon has traded yet."]
    lines += ["", "Small samples and heavy tails: read the sign across screens, not one screen's size. "
              "Returns are before trading costs."]
    return "\n".join(lines)
