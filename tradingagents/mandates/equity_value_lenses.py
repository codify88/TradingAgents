"""equity_value, with the judgement run through five investor lenses -- a trial.

The harness's first trial (docs/design/implementation-plan.md, stream F): does
asking the researchers and managers to test a value thesis through Graham,
Munger, Marks, Pabrai and Lynch make better calls than equity_value alone? Only
the evaluation harness can say, on the same screens against the same random
controls, so this overlay changes the judgement and nothing else: thesis,
analysts, screens, horizon and benchmark are equity_value's, derived rather
than copied so the two cannot drift apart.

Because it disagrees with its base on purpose, ``scores_as_base`` is False: its
decisions never stand in for equity_value's in screen-review.

The lenses are adapted, in our own words, from LLMQuant's investor-lenses
skills (MIT; see NOTICE). Run with ``--as equity_value_lenses``.
"""

from __future__ import annotations

from dataclasses import replace

from .equity_value import EQUITY_VALUE

_LENSES = (
    "Test the thesis through five lenses, one sentence each, citing the numbers the "
    "analysts produced rather than impressions. "
    "GRAHAM -- margin of safety: is the price clearly below a conservative value, and "
    "is the balance sheet strong enough that an ordinary downturn cannot force a "
    "permanent loss? An investment is safety of principal plus an adequate return on "
    "thorough analysis; anything else is speculation. "
    "MUNGER -- quality and inversion: are returns on capital high and durable, do "
    "earnings turn into cash, do management's incentives point the same way as "
    "shareholders'? Then invert: list the ways this purchase loses money permanently. "
    "MARKS -- the cycle: what is already priced in, and is sentiment around this name "
    "or its industry closer to euphoria or to despair? First-level thinking says the "
    "company is good; second-level thinking asks whether the price already says so. "
    "PABRAI -- asymmetry: if right, how much is made; if wrong, how much is lost? Prefer "
    "a small, bounded downside against a large upside, in a business simple enough to "
    "explain. "
    "LYNCH -- the story and its category: which kind of stock is this (slow grower, "
    "stalwart, fast grower, cyclical, turnaround, asset play), can the thesis be told "
    "in two minutes, and is the price reasonable for the growth? A falling price is a "
    "symptom: never average down into a deteriorating business."
)

_GUIDANCE = {
    "bull": (
        "Argue the case through the lenses that support it, and name them: Graham's "
        "margin of safety, Munger's quality, Pabrai's asymmetry, Lynch's category and "
        "story. " + _LENSES
    ),
    "bear": (
        "Lead with Munger's inversion -- how this loses money permanently -- and Marks' "
        "question of what the price already assumes. Call speculation by its name "
        "(Graham) and treat a falling price in a deteriorating business as a warning, "
        "not a bargain (Lynch). " + _LENSES
    ),
    "research_manager": (
        _LENSES + " Before recommending, state which lenses support the thesis and "
        "which object. A Buy or Overweight needs Graham's margin of safety and Munger's "
        "inversion both to hold; where the lenses disagree, name the evidence that "
        "would settle it."
    ),
    "portfolio_manager": (
        "Keep the rating consistent with the research manager's lens checklist: do not "
        "rate Buy or Overweight where Graham's margin of safety or Munger's inversion "
        "failed. In the executive summary, state Pabrai's asymmetry in one line: the "
        "loss if wrong against the gain if right."
    ),
}

EQUITY_VALUE_LENSES = replace(
    EQUITY_VALUE,
    name="equity_value_lenses",
    base=EQUITY_VALUE.name,
    scores_as_base=False,
    label="Long-Term Equity - Value, through five investor lenses (trial)",
    description=(
        EQUITY_VALUE.description
        + " The research and portfolio managers test each thesis through Graham, "
        "Munger, Marks, Pabrai and Lynch before rating it."
    ),
    agent_guidance={**EQUITY_VALUE.agent_guidance, **_GUIDANCE},
)
