"""The Mandate: what kind of investor the graph is running on behalf of.

``asset_type`` (upstream) answers *what* is being analysed. A mandate answers
*over what horizon, judged against what, framed how*. The two are orthogonal
and compose: ``asset_type="stock"`` + ``mandate="equity_value"``.

A mandate never replaces an upstream prompt. It renders a context block that
agents interpolate next to ``instrument_context``, so upstream prompt edits keep
merging cleanly (see docs/design/mandates.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .analysts.base import MandateAnalyst

# Trading days, not calendar days: the reflection loop counts price bars.
TRADING_DAYS_PER_YEAR = 252

# Graph names a mandate analyst may not take: upstream's analyst keys and every
# fixed node. A collision would silently overwrite an upstream node when the
# graph is built. Pinned against upstream by a drift test.
RESERVED_ANALYST_KEYS = frozenset({"market", "social", "news", "fundamentals"})
RESERVED_NODE_NAMES = frozenset({
    "Market Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst",
    "Bull Researcher", "Bear Researcher", "Research Manager", "Trader",
    "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst", "Portfolio Manager",
})

# The indicator menu the market analyst's prompt offers, which is also the set
# both data vendors implement (yfinance additionally has ``mfi``; Alpha Vantage
# does not, so it is left out). A shortlist naming anything else would send the
# model to call an indicator that fails at the tool, so it is rejected here.
MARKET_ANALYST_INDICATORS = frozenset({
    "close_50_sma", "close_200_sma", "close_10_ema",
    "macd", "macds", "macdh", "rsi",
    "boll", "boll_ub", "boll_lb", "atr", "vwma",
})


# The downstream agents a mandate may address by role in ``agent_guidance``.
DOWNSTREAM_AGENTS = frozenset({
    "bull", "bear", "research_manager", "trader",
    "aggressive", "conservative", "neutral", "portfolio_manager",
})


@dataclass(frozen=True)
class Mandate:
    """A named investment style with its own horizon, benchmark, and framing."""

    name: str
    label: str
    description: str
    asset_class: str = "equity"

    # --- outcome evaluation -------------------------------------------------
    # ``horizon_days`` is the primary grading window for the memory/reflection
    # loop. ``review_horizons_days`` are earlier checkpoints, so a 12-month call
    # still yields interim signal instead of going silent for a year.
    horizon_days: int = 5
    review_horizons_days: tuple[int, ...] = ()
    # None falls through to the config's ``benchmark_ticker`` / ``benchmark_map``.
    benchmark: str | None = None

    # --- prompt shaping -----------------------------------------------------
    thesis_frame: str = ""
    analyst_guidance: dict[str, str] = field(default_factory=dict)
    rating_guidance: str = ""
    disqualifiers: tuple[str, ...] = ()
    indicator_shortlist: tuple[str, ...] = ()
    # How the trader, risk debate and portfolio manager should define risk.
    # Upstream's debate implicitly treats risk as volatility; a long-horizon
    # value mandate means permanent loss of capital, which is a different debate.
    risk_frame: str = ""
    # Role-specific text for the downstream agents, keyed by DOWNSTREAM_AGENTS.
    # The shared mandate block tells every agent the same thing; this is where a
    # mandate narrows one role -- the Trader's stop-loss habit, the Conservative
    # debater's volatility framing -- without editing that agent's upstream prompt.
    agent_guidance: dict[str, str] = field(default_factory=dict)

    # --- extra analysts -------------------------------------------------------
    # Personas this mandate adds after upstream's analysts, each with its own
    # tools. Their reports reach every downstream agent via the mandate block.
    analysts: tuple[MandateAnalyst, ...] = ()

    def __post_init__(self):
        if self.horizon_days < 1:
            raise ValueError(
                f"mandate {self.name!r}: horizon_days must be >= 1, got {self.horizon_days}"
            )
        for h in self.review_horizons_days:
            if h < 1 or h >= self.horizon_days:
                raise ValueError(
                    f"mandate {self.name!r}: review horizon {h} must be in "
                    f"[1, {self.horizon_days}) -- the final horizon is graded "
                    f"by horizon_days itself, not listed as a review"
                )
        if list(self.review_horizons_days) != sorted(self.review_horizons_days):
            raise ValueError(
                f"mandate {self.name!r}: review_horizons_days must be ascending"
            )
        unknown = set(self.indicator_shortlist) - MARKET_ANALYST_INDICATORS
        if unknown:
            raise ValueError(
                f"mandate {self.name!r}: unknown indicator(s) in "
                f"indicator_shortlist: {', '.join(sorted(unknown))}"
            )
        unknown_agents = set(self.agent_guidance) - DOWNSTREAM_AGENTS
        if unknown_agents:
            raise ValueError(
                f"mandate {self.name!r}: unknown agent(s) in agent_guidance: "
                f"{', '.join(sorted(unknown_agents))}"
            )
        keys = [a.key for a in self.analysts]
        if len(keys) != len(set(keys)):
            raise ValueError(f"mandate {self.name!r}: duplicate analyst keys {keys}")
        for a in self.analysts:
            if a.key in RESERVED_ANALYST_KEYS:
                raise ValueError(
                    f"mandate {self.name!r}: analyst key {a.key!r} is upstream's"
                )
            if a.label in RESERVED_NODE_NAMES:
                raise ValueError(
                    f"mandate {self.name!r}: analyst label {a.label!r} is an upstream node"
                )

    def analyst(self, key: str) -> MandateAnalyst | None:
        """The mandate analyst with this key, or None."""
        return next((a for a in self.analysts if a.key == key), None)

    @property
    def all_horizons_days(self) -> tuple[int, ...]:
        """Every horizon to settle, earliest first, ending at the primary one."""
        return (*self.review_horizons_days, self.horizon_days)

    def guidance_for(self, analyst_key: str) -> str:
        """Extra system text for one analyst, or '' when the mandate is silent.

        The market analyst also gets the indicator shortlist. Upstream's prompt
        offers a twelve-indicator menu tuned for swing trading; left alone, a
        two-year value run spends its tool calls on RSI and Bollinger bands and
        then has to be told to ignore them. Narrowing the menu here keeps the
        upstream prompt untouched.
        """
        guidance = self.analyst_guidance.get(analyst_key, "")
        if analyst_key == "market" and self.indicator_shortlist:
            shortlist = (
                "Indicator selection: at this horizon only these indicators are "
                f"relevant -- {', '.join(self.indicator_shortlist)}. Choose from "
                "them instead of the full menu above, and do not call "
                "get_indicators for any other."
            )
            guidance = f"{guidance}\n\n{shortlist}" if guidance else shortlist
        return guidance


def render_mandate_context(mandate: Mandate | None) -> str:
    """Render the prompt block every agent interpolates.

    Returns '' for ``None`` so an un-mandated run reads exactly as it does
    upstream -- no stray headers, no behavioural drift.
    """
    if mandate is None:
        return ""

    years = mandate.horizon_days / TRADING_DAYS_PER_YEAR
    horizon = (
        f"{mandate.horizon_days} trading days (~{years:.1f} years)"
        if mandate.horizon_days >= TRADING_DAYS_PER_YEAR
        else f"{mandate.horizon_days} trading days"
    )

    lines = [
        f"INVESTMENT MANDATE: {mandate.label}",
        mandate.description,
        f"Evaluation horizon: {horizon}. Judge this position on how it performs "
        f"over that horizon, not on what it does next week.",
    ]
    if mandate.benchmark:
        lines.append(f"Benchmark: {mandate.benchmark}.")
    if mandate.thesis_frame:
        lines.append(f"What the thesis must establish: {mandate.thesis_frame}")
    if mandate.disqualifiers:
        lines.append(
            "Hard screens -- if any holds, the position cannot be rated Buy or "
            "Overweight no matter how strong the rest of the case. A tripped screen "
            "bars adding; on its own it is not a case for Underweight or Sell:\n"
            + "\n".join(f"  - {d}" for d in mandate.disqualifiers)
        )
    if mandate.rating_guidance:
        lines.append(f"Rating at this horizon: {mandate.rating_guidance}")
    if mandate.risk_frame:
        lines.append(f"How to judge risk: {mandate.risk_frame}")
    return "\n".join(lines)
