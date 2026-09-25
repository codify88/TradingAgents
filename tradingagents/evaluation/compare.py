"""Candidate against base, screen by screen, each over its own random controls."""

from __future__ import annotations

import math

from tradingagents.screener.manifest import load_manifests
from tradingagents.screener.review import _group, decision_logs

from .bar import ScreenComparison, Verdict, verdict
from .trials import Trial, save_trial, trials_in_style


def _exact(decision_mandate: str | None, mandate: str | None) -> bool:
    # Strict: the harness compares two arms, so neither may borrow the other's decisions.
    return (decision_mandate or "") == (mandate or "")


def compare(trial: Trial, config: dict) -> list[ScreenComparison]:
    manifests = {m.run_id: m for m in load_manifests(config)}
    entries = [e for log in decision_logs(config) for e in log.load_entries()]
    out = []
    for run_id in trial.screens:
        m = manifests.get(run_id)
        if m is None:
            continue
        arms = {}
        for mandate in (trial.base, trial.candidate):
            picks = _group("picks", m.pick_symbols, entries, m.as_of, mandate, match=_exact)
            ctrl = _group("control", m.control_symbols, entries, m.as_of, mandate, match=_exact)
            edge = (picks.mean_alpha - ctrl.mean_alpha
                    if picks.measurable and ctrl.measurable else float("nan"))
            arms[mandate] = (edge, (picks.settled, ctrl.settled))
        out.append(ScreenComparison(run_id, m.as_of, arms[trial.base][0], arms[trial.candidate][0],
                                    arms[trial.base][1], arms[trial.candidate][1]))
    return out


def evaluate(trial: Trial, config: dict, record: bool = True) -> tuple[list[ScreenComparison], Verdict]:
    comparisons = compare(trial, config)
    v = verdict(comparisons, trials_in_style(config, trial.style))
    if record and v.outcome != "not enough settled" and trial.status == "active":
        trial.status = "promoted" if v.outcome == "promote" else "archived"
        trial.verdicts.append({"outcome": v.outcome, "counted": v.counted, "wins": v.wins,
                               "required": v.required, "bar_version": v.bar_version})
        save_trial(config, trial)
    return comparisons, v


def render(trial: Trial, comparisons: list[ScreenComparison], v: Verdict) -> str:
    def pct(x):
        return "—" if math.isnan(x) else f"{x:+.1%}"

    lines = [f"# Trial {trial.id}: {trial.candidate} vs {trial.base} ({trial.kind}, {trial.style}; {trial.status})",
             "", "| Screen | As of | Base edge | Candidate edge | Base settled (p/c) | Candidate settled (p/c) | Counted |",
             "|---|---|---|---|---|---|---|"]
    for c in comparisons:
        lines.append(f"| {c.screen.rsplit('_', 1)[-1]} | {c.as_of} | {pct(c.base_edge)} | {pct(c.candidate_edge)} "
                     f"| {c.base_settled[0]}/{c.base_settled[1]} | {c.candidate_settled[0]}/{c.candidate_settled[1]} "
                     f"| {'yes' + (' (won)' if c.candidate_won else '') if c.counted else 'no'} |")
    lines += ["", v.render()]
    return "\n".join(lines)
