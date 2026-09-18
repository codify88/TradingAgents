"""Tier 3: run a screen's shortlist and control through the agent loop.

A screen names what is worth adjudicating; this adjudicates it. Picks and
controls go through the loop together, as one backtest sweep dated at the
screen's as-of date and run under the screen's mandate -- the only way
``screen-review``'s picks-vs-control comparison measures the screen rather
than the market, or the wrong mandate.

The sweep's run id is derived from the screen's, so the link between a screen
and its outcomes is permanent, and running a screen again resumes it: cells
already decided are skipped rather than paid for twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows.utils import get_current_date

from .manifest import ScreenManifest, load_manifests

# A full graph run with a mandate, measured: 462 s for KO with two analysts.
# Used only to size a run before it starts.
MINUTES_PER_CELL = 8

# The categories whose tools the market analyst prices a name with.
_PRICE_CATEGORIES = ("core_stock_apis", "technical_indicators")


def sweep_id(manifest: ScreenManifest) -> str:
    """The backtest run id a screen's names are adjudicated under.

    Compact because a run id becomes a path segment and is capped at 32
    characters: "screen_" plus a screen id is 40, which every real run would
    have been refused for. "scr_20220301_momentum_171242" keeps the date, the
    style and the screen's time suffix -- enough to find it by eye.
    """
    stamp = manifest.run_id.rsplit("_", 1)[-1]
    style = (manifest.mandate or "none").removeprefix("equity_")[:12]
    return f"scr_{manifest.as_of.replace('-', '')}_{style}_{stamp}"


def sweep_log_path(manifest: ScreenManifest, config: dict) -> Path:
    return Path(config["results_dir"]) / "backtest" / sweep_id(manifest) / "trading_memory.md"


def names(manifest: ScreenManifest) -> list[str]:
    """Picks then controls, each once."""
    return list(dict.fromkeys(manifest.pick_symbols + manifest.control_symbols))


def remaining(manifest: ScreenManifest, config: dict) -> list[str]:
    """Names the loop has not yet decided for this screen."""
    done = {
        e["ticker"]
        for e in TradingMemoryLog({"memory_log_path": str(sweep_log_path(manifest, config))}).load_entries()
        if e["date"] == manifest.as_of and (e.get("mandate") or "") == (manifest.mandate or "")
    }
    return [n for n in names(manifest) if n not in done]


def config_for(manifest: ScreenManifest, config: dict) -> dict:
    """The run config, with a price fallback for names that have since delisted.

    A historical screen can shortlist a company that no longer trades. Yahoo
    has dropped its history, so with a Yahoo-only vendor chain the market
    analyst would see no prices at all. Alpha Vantage keeps them; adding it
    *after* the configured vendors means it is consulted only when they have
    nothing, so a live screen's run is unchanged.
    """
    if manifest.as_of >= get_current_date():
        return config
    vendors = dict(config.get("data_vendors", {}))
    for category in _PRICE_CATEGORIES:
        chain = [v.strip() for v in str(vendors.get(category, "")).split(",") if v.strip()]
        if chain and "alpha_vantage" not in chain and "default" not in chain:
            vendors[category] = ",".join(chain + ["alpha_vantage"])
    return {**config, "data_vendors": vendors}


@dataclass
class Plan:
    manifest: ScreenManifest
    todo: list[str]

    @property
    def minutes(self) -> int:
        return len(self.todo) * MINUTES_PER_CELL


def plan(manifest: ScreenManifest, config: dict) -> Plan:
    return Plan(manifest, remaining(manifest, config))


def run(manifest: ScreenManifest, config: dict, selected_analysts=None, runner=None):
    """Adjudicate a screen's picks and controls; returns the backtest result.

    Cells already decided are skipped by the sweep itself, so an interrupted
    run resumes by being run again.
    """
    if runner is None:
        from tradingagents.backtest import run_backtest as runner

    kwargs = {"mandate": manifest.mandate or "", "run_id": sweep_id(manifest)}
    if selected_analysts:
        kwargs["selected_analysts"] = list(selected_analysts)
    return runner(names(manifest), [manifest.as_of], config_for(manifest, config), **kwargs)


def find(config: dict, screen_id: str) -> ScreenManifest:
    """A saved screen by full run id, by the short id ``screen-review`` prints, or "latest"."""
    manifests = load_manifests(config)
    if not manifests:
        raise ValueError("no screens have been saved yet; run `tradingagents screen` first")
    if screen_id == "latest":
        return max(manifests, key=lambda m: m.created)
    matches = [m for m in manifests if m.run_id == screen_id or m.run_id.endswith(f"_{screen_id}")]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"no screen matches {screen_id!r}")
    raise ValueError(f"{screen_id!r} matches {len(matches)} screens; use the full id: "
                     + ", ".join(m.run_id for m in matches))


def unfinished(config: dict, mandate: str | None = None) -> list[Plan]:
    """Every saved screen with names the loop has not decided yet, oldest first."""
    plans = [plan(m, config) for m in load_manifests(config, mandate)]
    return [p for p in plans if p.todo]
