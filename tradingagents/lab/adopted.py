"""Which lab variant a strategy's live screen uses, and who chose it when.

Nothing changes a live screen but an explicit adoption (``tradingagents lab
adopt``, or the Adopt button in Desk). Each adoption is appended with the
evidence that supported it -- the variant's lab figures and the lab's verdict at
the time -- so the screen config is versioned and the choice can be traced; the
latest one per strategy is in force.

Only the strategies in ``READ_LIVE`` have a live screen that reads its adoption
today. Adopting for the others is recorded (and shown as such), and takes effect
once their screen is wired to read it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from .panel import lab_dir
from .replay import SIGNALS, STRATEGIES, Variant, variants

# Before anything is adopted: the most liquid names, which takes no view.
DEFAULT_STANDARD = Variant("liquidity", 8, 5e6)

# Strategies whose live screen reads the adopted variant (screener/screen.py).
READ_LIVE = ("standard", "momentum", "value")
# Their screens examine a liquidity budget of names before ranking, so an
# adoptable variant must say how big (a /topN variant).
BUDGETED = ("momentum", "value")


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
    if not config.get("data_cache_dir"):
        return []  # no lab directory configured: nothing can have been adopted
    try:
        return [json.loads(x) for x in _path(config).read_text().splitlines() if x.strip()]
    except OSError:
        return []


def current(config: dict, strategy: str) -> dict | None:
    rows = [r for r in adoptions(config) if r["strategy"] == strategy]
    return rows[-1] if rows else None


def adopt(config: dict, strategy: str, variant_id: str, reason: str = "",
          evidence: dict | None = None) -> dict:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}")
    match = [v for v in variants(STRATEGIES[strategy]) if v.id == variant_id]
    if not match:
        raise ValueError(f"{variant_id!r} is not a {strategy} variant")
    v = match[0]
    if v.signal == "random":
        raise ValueError("the random ordering is the lab's null, not a screen")
    if strategy in BUDGETED and not v.pool:
        raise ValueError(f"the live {strategy} screen ranks within a liquidity budget; "
                         f"adopt a /topN variant, not {v.id}")
    row = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "strategy": strategy,
           "variant": v.id, "signal": v.signal, "picks": v.picks,
           "min_dollar_volume": v.min_dollar_volume, "pool": v.pool, "quality": v.quality,
           "reason": reason,
           "evidence": evidence if evidence is not None else evidence_for(config, strategy, v.id)}
    with _path(config).open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def evidence_for(config: dict, strategy: str, variant_id: str) -> dict | None:
    """The variant's figures and the verdict from the strategy's last lab run,
    or None if that run saved no results (runs before results were kept)."""
    from .report import load_results

    data = load_results(config, strategy)
    if not data:
        return None
    row = next((r for r in data["variants"] if r["variant"] == variant_id), None)
    verdict = data["verdict"]
    return {"lab_run": data["at"], "split": data["split"], "figures": row, "verdict": verdict,
            "lab_candidate": verdict["candidate"] == variant_id and verdict["passes"]}


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


@dataclass
class MandateOrdering:
    """An adopted variant as the value or momentum screen applies it."""

    strategy: str
    variant: str
    signal: str
    picks: int
    pool: int
    min_dollar_volume: float
    quality: bool
    source: str

    @property
    def describe(self) -> str:
        parts = [f"{self.signal} -- {SIGNALS[self.signal]}",
                 f"ranked within the {self.pool} most liquid names",
                 f"liquidity floor ${self.min_dollar_volume:,.0f}"]
        if not self.quality:
            parts.append("quality exclusions off")
        return "; ".join(parts) + f" ({self.variant}, {self.source})"


def strategy_key(mandate_name: str) -> str | None:
    return next((k for k, s in STRATEGIES.items() if s.name == mandate_name), None)


def mandate_ordering(config: dict, mandate_name: str) -> MandateOrdering | None:
    """The adoption in force for a mandate's screen, or None: the screen then runs
    exactly as it did before adoptions existed."""
    key = strategy_key(mandate_name)
    row = current(config, key) if key in BUDGETED else None
    if not row:
        return None
    v = next((x for x in variants(STRATEGIES[key]) if x.id == row["variant"]), None)
    if v is None or not v.pool:
        return None  # a variant the lab no longer defines is not applied silently
    return MandateOrdering(key, v.id, v.signal, v.picks, v.pool, v.min_dollar_volume, v.quality,
                           f"adopted {row['at'][:10]}")
