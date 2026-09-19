"""Lookup for the built-in mandates.

Kept separate from the definitions so adding a mandate is one import line here
and one new module, with no edits to the plumbing that consumes them.
"""

from __future__ import annotations

from .base import Mandate
from .equity_momentum import EQUITY_MOMENTUM
from .equity_momentum_leaps import EQUITY_MOMENTUM_LEAPS
from .equity_value import EQUITY_VALUE

# Wire value used in saved configs, the CLI, and the checkpoint signature.
# Empty string / None means "no mandate" and reproduces upstream behaviour
# exactly -- important so a fork merge never silently changes a plain run.
NO_MANDATE = ""

_MANDATES: dict[str, Mandate] = {
    m.name: m for m in (EQUITY_VALUE, EQUITY_MOMENTUM, EQUITY_MOMENTUM_LEAPS)
}


def list_mandates() -> list[Mandate]:
    """Every registered mandate, in registration order."""
    return list(_MANDATES.values())


def get_mandate(name: str | None) -> Mandate | None:
    """Resolve a mandate by wire name.

    Returns ``None`` for ``None`` or ``""`` so callers can treat "no mandate"
    and "upstream behaviour" as the same thing. An unknown name raises rather
    than falling back, so a typo in a config or a CLI flag fails at startup
    instead of quietly running the wrong style.
    """
    if not name:
        return None
    try:
        return _MANDATES[name]
    except KeyError:
        raise ValueError(
            f"Unknown mandate {name!r}. Available: {', '.join(_MANDATES)}"
        ) from None
