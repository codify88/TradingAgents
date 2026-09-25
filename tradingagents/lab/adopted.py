"""Which lab variant a strategy's live screen uses, and who chose it when.

Nothing changes a live screen but an explicit adoption (``tradingagents lab
adopt``, or approving the suggestion in Hermes). Each adoption is appended with
the verdict that supported it, so the screen config is versioned and the choice
can be traced; the latest one per strategy is in force.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from .panel import lab_dir
from .replay import SIGNALS, STRATEGIES, Variant, variants

# Before anything is adopted: the most liquid names, which takes no view.
DEFAULT_STANDARD = Variant("liquidity", 8, 5e6)


@dataclass
class Ordering:
    signal: str
    min_dollar_volume: float
    source: str               # "adopted <when>", "requested", or "default"

    @property
    def describe(self) -> str:
        return f"{self.signal} -- {SIGNALS[self.signal]}; liquidity floor ${self.min_dollar_volume:,.0f}"


def _path(config: dict):
    return lab_dir(config) / "adopted.jsonl"


def adoptions(config: dict) -> list[dict]:
    try:
        return [json.loads(x) for x in _path(config).read_text().splitlines() if x.strip()]
    except OSError:
        return []


def current(config: dict, strategy: str) -> dict | None:
    rows = [r for r in adoptions(config) if r["strategy"] == strategy]
    return rows[-1] if rows else None


def adopt(config: dict, strategy: str, variant_id: str, reason: str = "") -> dict:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}")
    match = [v for v in variants(STRATEGIES[strategy]) if v.id == variant_id]
    if not match:
        raise ValueError(f"{variant_id!r} is not a {strategy} variant")
    v = match[0]
    if v.signal == "random":
        raise ValueError("the random ordering is the lab's null, not a screen")
    row = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "strategy": strategy,
           "variant": v.id, "signal": v.signal, "picks": v.picks,
           "min_dollar_volume": v.min_dollar_volume, "reason": reason}
    with _path(config).open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def standard_ordering(config: dict, requested: str | None = None) -> Ordering:
    if requested:
        if requested not in SIGNALS:
            raise ValueError(f"unknown ordering {requested!r}; choose from {', '.join(SIGNALS)}")
        return Ordering(requested, DEFAULT_STANDARD.min_dollar_volume, "requested")
    row = current(config, "standard")
    if row:
        return Ordering(row["signal"], float(row["min_dollar_volume"]), f"adopted {row['at'][:10]}")
    return Ordering(DEFAULT_STANDARD.signal, DEFAULT_STANDARD.min_dollar_volume,
                    "default: nothing adopted from the lab yet")
