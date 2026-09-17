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

# Trading days, not calendar days: the reflection loop counts price bars.
TRADING_DAYS_PER_YEAR = 252


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

    @property
    def all_horizons_days(self) -> tuple[int, ...]:
        """Every horizon to settle, earliest first, ending at the primary one."""
        return (*self.review_horizons_days, self.horizon_days)

    def guidance_for(self, analyst_key: str) -> str:
        """Extra system text for one analyst, or '' when the mandate is silent."""
        return self.analyst_guidance.get(analyst_key, "")


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
            "Overweight no matter how strong the rest of the case:\n"
            + "\n".join(f"  - {d}" for d in mandate.disqualifiers)
        )
    if mandate.rating_guidance:
        lines.append(f"Rating at this horizon: {mandate.rating_guidance}")
    return "\n".join(lines)
