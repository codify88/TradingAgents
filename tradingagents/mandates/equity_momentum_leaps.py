"""equity_momentum, with the position optionally held through a LEAPS call.

An overlay, not a new strategy (docs/design/leaps.md): everything that decides
direction -- thesis, analysts, screens, rating guidance, horizon -- is
equity_momentum's, derived rather than copied so the two cannot drift apart.
The overlay adds the LEAPS Analyst and one decision at the end: stock or call.
Run with ``--mandate equity_momentum_leaps``; its decisions are graded on the
same 126-day horizon, and the instrument choice separately (build step 4).
"""

from __future__ import annotations

from dataclasses import replace

from .analysts.leaps import LEAPS_ANALYSTS
from .equity_momentum import EQUITY_MOMENTUM

_INSTRUMENT_RULE = (
    "Set the Instrument field. Call only when the rating is Buy or Overweight "
    "and the LEAPS Analyst's screens are all CLEAR; every other case is Stock, "
    "including every Hold, Underweight and Sell. A Call is always the contract "
    "in the LEAPS Analyst's report -- never another strike or expiry. The "
    "rating itself is decided exactly as it would be without a call on offer: "
    "the instrument is how the position is held, not whether."
)

EQUITY_MOMENTUM_LEAPS = replace(
    EQUITY_MOMENTUM,
    name="equity_momentum_leaps",
    base=EQUITY_MOMENTUM.name,
    label="Long-Term Equity - Growth / Momentum, held through LEAPS",
    description=(
        EQUITY_MOMENTUM.description
        + " A position may be held through a long-dated call instead of the "
        "shares, when the call's cost and risk make it the better instrument."
    ),
    analysts=EQUITY_MOMENTUM.analysts + LEAPS_ANALYSTS,
    agent_guidance={
        **EQUITY_MOMENTUM.agent_guidance,
        "portfolio_manager": _INSTRUMENT_RULE,
    },
)
