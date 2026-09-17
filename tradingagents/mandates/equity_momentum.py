"""Growth / momentum mandate (trend persistence and earnings revisions)."""

from __future__ import annotations

from .base import Mandate

EQUITY_MOMENTUM = Mandate(
    name="equity_momentum",
    label="Long-Term Equity - Growth / Momentum",
    description=(
        "You are riding demonstrated business and price momentum in a growing "
        "company, holding for months rather than years, and exiting on a "
        "defined break in the trend or the fundamentals. You are paying up for "
        "acceleration; the discipline that makes this work is the exit rule, "
        "not the entry."
    ),
    asset_class="equity",
    # Classic 12-1 momentum holds 3-12 months; six months sits in the pocket.
    horizon_days=126,
    review_horizons_days=(21, 63),
    # None -> config benchmark_map (see the note in equity_value). Set MTUM
    # here to grade within the momentum factor instead of against the market.
    benchmark=None,
    thesis_frame=(
        "that growth is real, accelerating or durably high, and better than the "
        "market currently expects; that the price trend confirms it rather than "
        "contradicting it; and -- stated up front -- the specific conditions "
        "that would prove the thesis wrong and trigger an exit. A thesis with "
        "no kill criteria is not a momentum thesis."
    ),
    analyst_guidance={
        "market": (
            "Trend structure is the primary signal at this horizon. Emphasise: "
            "trailing returns over 1, 3, 6, and 12 months and how they rank "
            "against the benchmark and sector; position relative to the 52-week "
            "high; whether price is above a rising 50- and 200-day average; "
            "whether recent consolidation is an orderly base or a distribution "
            "top; and whether advances come on expanding volume. Distinguish "
            "'extended but intact' from 'broken'. Give a concrete invalidation "
            "level, not a vague caution."
        ),
        "fundamentals": (
            "Momentum without fundamental support is a trade waiting to "
            "unwind. Emphasise the rate of change rather than the level: "
            "revenue and EPS growth and whether it is accelerating or "
            "decelerating; gross margin direction; whether growth is funded by "
            "operations or by dilution and debt; and the trajectory of "
            "analyst estimate revisions -- upward revisions outnumbering "
            "downward is the confirming signal, a downward turn is an early "
            "warning that usually precedes the price. Flag decelerating growth "
            "explicitly even when the chart still looks strong."
        ),
        "news": (
            "Look for catalysts that extend or terminate the trend: earnings "
            "surprises and guidance changes, product cycles, competitive "
            "entry, regulatory action, and index or flow events. Rank items by "
            "whether they change the expected growth rate, and note how much "
            "of the reaction the price has already absorbed."
        ),
        "social": (
            "At this horizon sentiment is partly a confirming input -- "
            "attention sustains trends -- but euphoria at a price extreme is a "
            "late-stage warning. Report both the direction and the intensity, "
            "and say which stage the narrative appears to be in."
        ),
    },
    rating_guidance=(
        "Buy requires confirmed price momentum, fundamental growth that "
        "supports it, and an invalidation level that is close enough to make "
        "the risk tolerable. Overweight fits a constructive trend with one "
        "weak leg. Hold covers extended-but-intact positions where a new entry "
        "is not attractive. Underweight and Sell belong where the trend has "
        "broken or where estimate revisions have turned down -- act on "
        "deterioration early rather than waiting for confirmation, because at "
        "this horizon the cost of being late exceeds the cost of being early."
    ),
    disqualifiers=(
        "Price momentum and fundamental momentum point in opposite directions "
        "with no credible explanation.",
        "Growth is decelerating while the multiple is still expanding.",
        "No defensible invalidation level can be identified.",
        "The move is driven by a one-off event already fully reflected in the "
        "price.",
    ),
    indicator_shortlist=(
        "close_50_sma", "close_200_sma", "close_10_ema", "macd", "macds", "rsi", "atr", "vwma",
    ),
)
