"""The trial registry: every candidate the harness has tested, and on what.

A trial is a candidate mandate -- a trial overlay (investor lenses, a learned
playbook) or an external strategy written up as a mandate -- run against its
base mandate on screens chosen **before** it runs. One registry for every kind,
so the promotion bar can count everything ever tried in a style: testing many
candidates and keeping the best finds noise, and the count is the defence.

Stored as JSON under ``results_dir/trials/``. A trial is never deleted; it is
promoted or archived, with the verdict and the bar version it was judged under.

Design: docs/design/implementation-plan.md, stream D.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

KINDS = ("overlay", "playbook", "strategy")
STATUSES = ("active", "promoted", "archived")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")


@dataclass
class Trial:
    id: str
    kind: str
    style: str                 # e.g. "value": the bar counts trials per style
    base: str                  # the base mandate, e.g. "equity_value"
    candidate: str             # the candidate mandate, e.g. "equity_value_lenses"
    screens: list[str]         # saved-screen run ids, fixed at registration
    created: str
    status: str = "active"
    # A playbook is distilled from outcomes up to a date; its screens must be
    # dated after that, or the test grades it on the outcomes it was taught.
    evidence_through: str | None = None
    notes: str = ""
    verdicts: list[dict] = field(default_factory=list)


def _dir(config: dict) -> Path:
    return Path(config["results_dir"]) / "trials"


def load_trials(config: dict) -> list[Trial]:
    out = []
    for path in sorted(_dir(config).glob("*.json")):
        try:
            out.append(Trial(**json.loads(path.read_text(encoding="utf-8"))))
        except (ValueError, TypeError):
            continue
    return out


def get_trial(config: dict, trial_id: str) -> Trial:
    for t in load_trials(config):
        if t.id == trial_id:
            return t
    raise ValueError(f"no trial {trial_id!r}")


def save_trial(config: dict, trial: Trial) -> Path:
    path = _dir(config) / f"{trial.id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(trial), indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def trials_in_style(config: dict, style: str) -> int:
    """Every trial ever registered in ``style``, whatever became of it."""
    return sum(1 for t in load_trials(config) if t.style == style)


def register(config: dict, *, trial_id: str, kind: str, style: str, base: str, candidate: str,
             screens: list[str], evidence_through: str | None = None, notes: str = "",
             today: str | None = None) -> Trial:
    """Register a trial, refusing anything that would make its test unfair."""
    from tradingagents.mandates.registry import get_mandate
    from tradingagents.screener.run import find

    if not _ID.match(trial_id):
        raise ValueError("trial id: 3-64 lowercase letters, digits, - or _")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if any(t.id == trial_id for t in load_trials(config)):
        raise ValueError(f"trial {trial_id!r} already exists")
    cand = get_mandate(candidate)
    if cand is None or get_mandate(base) is None:
        raise ValueError("candidate and base must both be registered mandates")
    if kind != "strategy" and cand.base != base:
        raise ValueError(f"{candidate} is not an overlay of {base}")
    if cand.base == base and cand.scores_as_base:
        raise ValueError(f"{candidate} scores as its base; a trial overlay must set scores_as_base=False "
                         "or its decisions would be mixed into the base's")
    if not screens:
        raise ValueError("a trial needs its screens fixed before it runs")
    run_ids = []
    for s in screens:
        m = find(config, s)
        if m.mandate != base:
            raise ValueError(f"screen {m.run_id} is a {m.mandate} screen, not {base}")
        if evidence_through and m.as_of <= evidence_through:
            raise ValueError(f"screen {m.run_id} (as of {m.as_of}) is not after the evidence "
                             f"({evidence_through}): the test would be in sample")
        run_ids.append(m.run_id)
    trial = Trial(id=trial_id, kind=kind, style=style, base=base, candidate=candidate,
                  screens=run_ids, created=today or date.today().isoformat(),
                  evidence_through=evidence_through, notes=notes)
    save_trial(config, trial)
    return trial
