"""The mandate playbook: rules distilled from lessons, each carrying its evidence.

A playbook is knowledge with a date. Each version records ``available_at`` --
the latest date among the outcomes its rules cite -- so a historical run can
load only a version that existed before its analysis date (wave 4, J.2).

Rule states:

- ``candidate``: proposed by the distill, below the evidence bar; never reaches
  an agent;
- ``eligible``: over the evidence bar (gate.py); a playbook of eligible rules is
  what the evaluation harness tests as an overlay (wave 4);
- ``promoted``: its playbook beat the base mandate through the harness;
- ``archived``: withdrawn, with its record -- never deleted;
- ``pinned``: written by a person; the loop never changes it.

Rules follow Hermes' skill-writing discipline: a generalisable rule plus one
clause of why; no incident narration, tickers or dates in the rule itself (the
evidence ids carry those); the same lesson twice is one rule.

Stored under ``results_dir/playbooks/<mandate>/``: ``vNNN.json`` and a rendered
``vNNN.md``; every version is kept.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATES = ("candidate", "eligible", "promoted", "archived", "pinned")


@dataclass
class Rule:
    id: str
    text: str
    why: str
    supports: list[str] = field(default_factory=list)      # lesson ids
    contradicts: list[str] = field(default_factory=list)   # lesson ids
    state: str = "candidate"


@dataclass
class Playbook:
    mandate: str
    version: int
    created: str
    available_at: str | None
    rules: list[Rule]
    bar_version: str = ""

    def active(self) -> list[Rule]:
        return [r for r in self.rules if r.state in ("eligible", "promoted", "pinned")]

    def render(self) -> str:
        lines = [f"# Playbook: {self.mandate} (v{self.version})", "",
                 f"Created {self.created}; built from outcomes known by {self.available_at or 'n/a'}.", ""]
        for state in ("pinned", "promoted", "eligible", "candidate", "archived"):
            rules = [r for r in self.rules if r.state == state]
            if not rules:
                continue
            lines += [f"## {state.capitalize()} ({len(rules)})", ""]
            for r in rules:
                lines.append(f"- **{r.text}** -- {r.why} "
                             f"_(for {len(r.supports)}, against {len(r.contradicts)}; {r.id})_")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _dir(config: dict, mandate: str) -> Path:
    return Path(config["results_dir"]) / "playbooks" / mandate


def latest(config: dict, mandate: str, available_by: str | None = None) -> Playbook | None:
    """The newest version, or the newest available by ``available_by``."""
    versions = sorted(_dir(config, mandate).glob("v*.json"))
    for path in reversed(versions):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["rules"] = [Rule(**r) for r in data["rules"]]
        pb = Playbook(**data)
        if available_by is None or (pb.available_at or "") <= available_by:
            return pb
    return None


def save(config: dict, pb: Playbook) -> Path:
    d = _dir(config, pb.mandate)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"v{pb.version:03d}.json"
    if path.exists():
        raise FileExistsError(f"{path} exists: versions are never overwritten")
    path.write_text(json.dumps(asdict(pb), indent=2), encoding="utf-8")
    (d / f"v{pb.version:03d}.md").write_text(pb.render(), encoding="utf-8")
    return path
