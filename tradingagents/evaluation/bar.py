"""The promotion bar, in one place, and the comparison it judges.

For each of a trial's screens the harness measures two edges on the same names:
the base mandate's picks minus its controls, and the candidate's picks minus
its controls. A screen counts once both arms have enough settled cells on each
side. The candidate is promoted when it beats the base on enough of the counted
screens, and archived when it does not.

The bar is provisional (decision 4, 2026-09-25): revisit it if nothing clears
for months while cells settle (too strict), or promoted candidates stop beating
the base on later screens (too loose). Every verdict records ``BAR_VERSION``, so
changing the bar never rewrites a past promotion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

BAR_VERSION = "2026-09-25.1"
MIN_SCREENS = 4
MIN_SETTLED_PER_SIDE = 5
MIN_WIN_FRACTION = 0.75
# One more screen for every five trials already run in the style: the more that
# has been tried, the more a single good-looking result is likely to be luck.
EXTRA_SCREEN_PER_TRIALS = 5


@dataclass
class ScreenComparison:
    screen: str
    as_of: str
    base_edge: float
    candidate_edge: float
    base_settled: tuple[int, int]        # (picks, controls)
    candidate_settled: tuple[int, int]

    @property
    def counted(self) -> bool:
        return (min(self.base_settled) >= MIN_SETTLED_PER_SIDE
                and min(self.candidate_settled) >= MIN_SETTLED_PER_SIDE
                and not math.isnan(self.base_edge) and not math.isnan(self.candidate_edge))

    @property
    def candidate_won(self) -> bool:
        return self.counted and self.candidate_edge > self.base_edge


@dataclass
class Verdict:
    outcome: str            # "not enough settled" | "promote" | "archive"
    counted: int
    wins: int
    required: int
    bar_version: str = BAR_VERSION

    def render(self) -> str:
        if self.outcome == "not enough settled":
            return (f"Not enough settled cells yet: {self.counted} of the {self.required} screens the bar "
                    f"needs have at least {MIN_SETTLED_PER_SIDE} settled picks and controls in both arms. "
                    f"This is the expected answer while outcomes settle.")
        verb = "Promote" if self.outcome == "promote" else "Archive"
        return (f"{verb}: the candidate beat the base on {self.wins} of {self.counted} counted screens "
                f"(bar: {MIN_WIN_FRACTION:.0%} of at least {self.required}; bar {self.bar_version}).")


def required_screens(trials_in_style: int) -> int:
    return MIN_SCREENS + max(trials_in_style - 1, 0) // EXTRA_SCREEN_PER_TRIALS


def verdict(comparisons: list[ScreenComparison], trials_in_style: int) -> Verdict:
    required = required_screens(trials_in_style)
    counted = [c for c in comparisons if c.counted]
    wins = sum(c.candidate_won for c in counted)
    if len(counted) < required:
        return Verdict("not enough settled", len(counted), wins, required)
    outcome = "promote" if wins / len(counted) >= MIN_WIN_FRACTION else "archive"
    return Verdict(outcome, len(counted), wins, required)
