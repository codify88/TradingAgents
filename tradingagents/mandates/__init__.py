"""Investment mandates: horizon, benchmark, and framing for a run.

See docs/design/mandates.md for why this exists and how it composes with
upstream's ``asset_type``.
"""

from .base import TRADING_DAYS_PER_YEAR, Mandate, render_mandate_context
from .equity_momentum import EQUITY_MOMENTUM
from .equity_value import EQUITY_VALUE
from .registry import NO_MANDATE, get_mandate, list_mandates

__all__ = [
    "EQUITY_MOMENTUM",
    "EQUITY_VALUE",
    "NO_MANDATE",
    "TRADING_DAYS_PER_YEAR",
    "Mandate",
    "get_mandate",
    "list_mandates",
    "render_mandate_context",
]
