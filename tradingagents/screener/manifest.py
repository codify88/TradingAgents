"""A screen run, written down so it can be judged later.

A screener is only worth having if you can tell whether it beat picking
eligible names at random. That question can only be answered after the fact,
which means every run has to record what it chose, what it rejected and why,
and which names were the control -- at the time, before any outcome is known.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class ScreenManifest:
    """Everything needed to reproduce and later score one screen run."""

    run_id: str
    mandate: str
    as_of: str
    created: str
    universe_size: int
    tiers: list[dict]                     # name, kept, dropped, reason counts
    ordering_signal: str                  # declared, so the bias introduced is visible
    picks: list[dict]                     # symbol, rank, ordering value
    controls: list[dict]                  # symbol, ordering value (rank is meaningless)
    control_seed: int
    eligible_count: int
    notes: list[str] = field(default_factory=list)

    @property
    def pick_symbols(self) -> list[str]:
        return [p["symbol"] for p in self.picks]

    @property
    def control_symbols(self) -> list[str]:
        return [c["symbol"] for c in self.controls]


def _dir(config: dict) -> Path:
    path = Path(config["results_dir"]) / "screens"
    path.mkdir(parents=True, exist_ok=True)
    return path


def make_run_id(mandate: str, as_of: str) -> str:
    return f"{as_of}_{mandate or 'none'}_{datetime.now().strftime('%H%M%S')}"


def save_manifest(manifest: ScreenManifest, config: dict) -> Path:
    path = _dir(config) / f"{manifest.run_id}.json"
    path.write_text(json.dumps(asdict(manifest), indent=2), encoding="utf-8")
    return path


def load_manifests(config: dict, mandate: str | None = None) -> list[ScreenManifest]:
    """Every saved screen, oldest first; unreadable files are skipped, not fatal."""
    out = []
    for path in sorted(_dir(config).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            manifest = ScreenManifest(**data)
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
        if mandate is None or manifest.mandate == mandate:
            out.append(manifest)
    return out
