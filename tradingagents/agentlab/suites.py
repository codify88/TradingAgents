"""Suites: fixed sets of historical cases a variant is tested on.

A suite is a folder of saved screens (picks and random controls), so every
variant is judged on the same names and dates, and the picks-vs-controls
comparison the screener lab uses is available here too. Built from the
standard screen on chosen dates -- which costs price requests only, no model
calls -- or imported from screens already run (a pilot's).

Dates should fall after the testing model's training cutoff: a model that has
read about a week cannot be tested on it. ``MODEL_CUTOFFS`` holds the published
training-data cutoffs (platform.claude.com, checked 2026-09-26).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

MODEL_CUTOFFS = {
    "claude-haiku-4-5": "2025-07-31",
    "claude-sonnet-5": "2026-01-31",
    "claude-opus-5-5": "2026-06-30",
    "claude-fable-5-1": "2026-06-30",
}
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


@dataclass
class Suite:
    name: str
    description: str
    created: str
    dates: list[str]
    cases: int                 # names across all screens (picks + controls)
    picks: int
    controls: int
    status: str = "ready"      # building | ready | failed
    total: int = 0             # dates asked for (while building)
    done: int = 0              # dates screened so far
    error: str = ""

# Measured 2026-09-26: one standard screen over ~5,000 stored histories.
SECONDS_PER_DATE = 55


def _root(config: dict) -> Path:
    d = Path(config["data_cache_dir"]) / "agentlab" / "suites"
    d.mkdir(parents=True, exist_ok=True)
    return d


def suite_dir(config: dict, name: str) -> Path:
    if not NAME.match(name):
        raise ValueError(f"no suite {name!r}")
    return _root(config) / name


def manifests(config: dict, name: str) -> list:
    from tradingagents.screener.manifest import ScreenManifest

    d = suite_dir(config, name) / "screens"
    return [ScreenManifest(**json.loads(p.read_text())) for p in sorted(d.glob("*.json"))]


def _write_meta(config: dict, name: str, description: str, status: str = "ready", total: int = 0,
                error: str = "") -> Suite:
    ms = manifests(config, name)
    s = Suite(name, description, datetime.now(UTC).isoformat(timespec="seconds"),
              sorted({m.as_of for m in ms}),
              sum(len(m.pick_symbols) + len(m.control_symbols) for m in ms),
              max((len(m.pick_symbols) for m in ms), default=0),
              max((len(m.control_symbols) for m in ms), default=0),
              status=status, total=total or len(ms), done=len(ms), error=error)
    (suite_dir(config, name) / "suite.json").write_text(json.dumps(asdict(s), indent=2))
    return s


def exists(config: dict, name: str) -> bool:
    return suite_dir(config, name).exists()


def all_suites(config: dict) -> list[Suite]:
    out = []
    for p in sorted(_root(config).glob("*/suite.json")):
        try:
            s = Suite(**json.loads(p.read_text()))
        except (TypeError, ValueError):
            continue
        if s.status == "building":  # progress from the screens saved so far, not the last write
            s.done = len(list((p.parent / "screens").glob("*.json")))
        out.append(s)
    return out


def weekly_dates(start: str, end: str, count: int) -> list[str]:
    """``count`` Fridays spread evenly from ``start`` to ``end``."""
    fridays = pd.date_range(start, end, freq="W-FRI")
    if len(fridays) == 0:
        return []
    idx = sorted({round(i * (len(fridays) - 1) / max(count - 1, 1)) for i in range(min(count, len(fridays)))})
    return [fridays[i].strftime("%Y-%m-%d") for i in idx]


def build(config: dict, name: str, dates: list[str], picks: int = 8, controls: int = 4,
          description: str = "", seed: int = 11, screen=None) -> Suite:
    """A suite from the standard screen on each date (price requests, no model calls)."""
    from tradingagents.screener.manifest import save_manifest

    if screen is None:
        from tradingagents.screener.screen import run_screen as screen
    d = suite_dir(config, name)
    if d.exists():
        raise ValueError(f"suite {name!r} exists; suites are fixed once built")
    (d / "screens").mkdir(parents=True)
    cfg = {**config, "results_dir": str(d)}
    description = description or f"Standard screen, {len(dates)} dates, {picks} picks + {controls} controls"
    # Visible from the first moment, with progress, so nobody builds it twice.
    _write_meta(config, name, description, status="building", total=len(dates))
    try:
        for i, day in enumerate(dates):
            result = screen("", day, cfg, picks=picks, controls=controls, control_seed=seed + i)
            if result.manifest.picks:
                save_manifest(result.manifest, cfg)
            _write_meta(config, name, description, status="building", total=len(dates))
    except Exception as exc:
        _write_meta(config, name, description, status="failed", total=len(dates),
                    error=f"{type(exc).__name__}: {exc}"[:300])
        raise
    return _write_meta(config, name, description, total=len(dates))


def import_screens(config: dict, name: str, screen_files: list[Path], description: str = "") -> Suite:
    d = suite_dir(config, name)
    if d.exists():
        raise ValueError(f"suite {name!r} exists; suites are fixed once built")
    (d / "screens").mkdir(parents=True)
    for f in screen_files:
        shutil.copy(f, d / "screens" / Path(f).name)
    return _write_meta(config, name, description or f"Imported {len(screen_files)} screen(s)")


def cutoff_warning(suite: Suite, models: list[str]) -> str | None:
    """Why this suite may flatter these models: dates they may have read about."""
    for m in models:
        cut = next((c for k, c in MODEL_CUTOFFS.items() if m.startswith(k)), None)
        if cut and suite.dates and min(suite.dates) <= cut:
            return (f"{m} was trained on data to {cut}; {sum(d <= cut for d in suite.dates)} of this "
                    f"suite's {len(suite.dates)} dates fall before that, so it may know what happened next")
    return None
