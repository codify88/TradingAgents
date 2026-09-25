"""What the nightly adjudication budget goes to.

Decision 5 (2026-09-25): five names a night -- two for the screen backlog, three
for active trials. Trial slots go round-robin across active trials, oldest
screen first within each; slots the trials cannot use go to the backlog, and the
backlog's go back to the trials if it has run dry, so no slot is wasted while
there is work.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_NAMES = 5
DEFAULT_BACKLOG = 2


@dataclass
class Slot:
    plan: object        # screener.run.Plan
    run_as: str | None  # the candidate mandate, or None for the base
    names: int
    trial: str | None = None


def plan_night(config: dict, mandate: str, max_names: int = DEFAULT_MAX_NAMES,
               backlog: int = DEFAULT_BACKLOG) -> list[Slot]:
    from tradingagents.screener import run as screen_run

    from .trials import load_trials

    backlog = min(backlog, max_names)
    queues: list[list[tuple[str, str, object]]] = []  # per trial: (trial id, candidate, plan)
    for t in load_trials(config):
        if t.status != "active" or t.base != mandate:
            continue
        q = []
        for run_id in t.screens:
            try:
                p = screen_run.plan(screen_run.find(config, run_id), config, t.candidate)
            except ValueError:
                continue
            if p.todo:
                q.append((t.id, t.candidate, p))
        if q:
            queues.append(q)
    backlog_plans = [p for p in screen_run.unfinished(config, mandate) if p.todo]

    slots: list[Slot] = []
    left_in = {}  # id(plan) -> names not yet allocated

    def give(plan, run_as, trial) -> bool:
        key = id(plan)
        left_in.setdefault(key, len(plan.todo))
        if left_in[key] <= 0:
            return False
        left_in[key] -= 1
        slot = next((s for s in slots if s.plan is plan), None)
        if slot is None:
            slots.append(Slot(plan, run_as, 1, trial))
        else:
            slot.names += 1
        return True

    def round_robin(budget: int) -> int:
        while budget > 0:
            progressed = False
            for q in queues:
                while q and not give(q[0][2], q[0][1], q[0][0]):
                    q.pop(0)  # that screen is fully allocated
                if q:
                    budget -= 1
                    progressed = True
                    if budget == 0:
                        break
            if not progressed:
                break
        return budget

    left = backlog + round_robin(max_names - backlog)
    for p in backlog_plans:
        while left > 0 and give(p, None, None):
            left -= 1
    round_robin(left)
    return slots
