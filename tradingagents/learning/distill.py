"""The weekly distill: lessons -> proposed playbook edits -> checked -> gated.

One model call per mandate reads the lessons known so far and the current
playbook, and proposes edits. The model's answer is never trusted as is: a
deterministic check drops what breaks the rules (uncited, citing lessons that do
not exist, naming tickers or dates, touching a pinned rule), and the evidence
gate decides which rules become ``eligible``. Nothing here reaches an agent;
wave 4 tests an eligible playbook through the evaluation harness first.

Design: docs/design/hermes.md, part 2 (distill, evidence gate).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from . import gate
from .lessons import Lesson, load_lessons
from .playbook import Playbook, Rule, latest, save

MAX_LESSONS = 80
MAX_LESSON_CHARS = 700
MAX_RULE_CHARS = 300
MAX_WHY_CHARS = 240
_DATE = re.compile(r"\b(19|20)\d{2}(-\d{2})?\b")


class RuleEdit(BaseModel):
    action: Literal["add", "strengthen", "archive"]
    rule_id: str | None = Field(None, description="For strengthen/archive: the existing rule's id.")
    text: str = Field(description="The rule: one imperative sentence that generalises. No tickers, no dates.")
    why: str = Field(description="One clause: the mechanism that makes the rule true.")
    supports: list[str] = Field(default_factory=list, description="Lesson ids whose outcome supports the rule.")
    contradicts: list[str] = Field(default_factory=list, description="Lesson ids whose outcome contradicts it.")


class Proposal(BaseModel):
    edits: list[RuleEdit] = Field(default_factory=list)


WRITING_RULES = """How to write a playbook rule (adapted from Hermes' skill-writing rules):
- A rule is a generalisable instruction plus one clause of WHY -- the mechanism.
  Not a narrative of what happened to one company.
- No tickers, company names or dates in the rule or its why; the lesson ids you
  cite carry those.
- The same lesson learned twice is ONE rule: strengthen an existing rule (cite
  the new lessons) rather than adding a near-copy.
- Cite every lesson whose OUTCOME supports the rule, and every one whose outcome
  contradicts it. Honest contradictions are required, not optional.
- Propose archiving a rule when the evidence now runs against it.
- Prefer few, strong rules to many weak ones. Propose nothing if nothing is
  supported."""


def _prompt(mandate: str, lessons: list[Lesson], current: Playbook | None) -> str:
    rules = "\n".join(f"- [{r.id}] ({r.state}) {r.text} -- {r.why} (for {len(r.supports)}, "
                      f"against {len(r.contradicts)})" for r in (current.rules if current else [])) or "(none yet)"
    body = "\n\n".join(
        f"[{x.id}] rating {x.rating}; {'settled' if x.final else 'interim'} at {x.horizon_days} trading days; "
        f"alpha {x.alpha}\n{x.text[:MAX_LESSON_CHARS]}" for x in lessons)
    return (f"You maintain the investment playbook for the {mandate} mandate: rules that improve "
            f"future decisions, learned from how past decisions turned out.\n\n{WRITING_RULES}\n\n"
            f"Current rules:\n{rules}\n\nLessons (id, outcome, what the reflection concluded):\n\n{body}\n\n"
            "Propose edits to the playbook.")


@dataclass
class DistillResult:
    playbook: Playbook | None
    accepted: list[RuleEdit] = field(default_factory=list)
    rejected: list[tuple[RuleEdit, str]] = field(default_factory=list)
    gate_notes: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        lines = []
        if self.playbook:
            eligible = sum(r.state == "eligible" for r in self.playbook.rules)
            lines.append(f"Playbook {self.playbook.mandate} v{self.playbook.version}: {len(self.playbook.rules)} rules, "
                         f"{eligible} eligible; built from outcomes known by {self.playbook.available_at}.")
        lines.append(f"Accepted {len(self.accepted)} edit(s), rejected {len(self.rejected)}.")
        lines += [f"- rejected ({why}): {e.text[:100]}" for e, why in self.rejected]
        lines += [f"- {rid}: {note}" for rid, note in self.gate_notes.items()]
        return "\n".join(lines)


def check(edit: RuleEdit, lessons: dict[str, Lesson], current: dict[str, Rule]) -> tuple[RuleEdit | None, str]:
    """Drop what breaks the rules; keep only citations of lessons that exist."""
    tickers = {x.ticker for x in lessons.values()}
    words = set(re.findall(r"\b[A-Z][A-Z.]{0,5}\b", f"{edit.text} {edit.why}"))
    if words & tickers:
        return None, f"names a ticker ({', '.join(sorted(words & tickers))})"
    if _DATE.search(edit.text) or _DATE.search(edit.why):
        return None, "names a date"
    if len(edit.text) > MAX_RULE_CHARS or len(edit.why) > MAX_WHY_CHARS or not edit.text.strip():
        return None, "too long or empty"
    supports = [i for i in dict.fromkeys(edit.supports) if i in lessons]
    contradicts = [i for i in dict.fromkeys(edit.contradicts) if i in lessons and i not in supports]
    if edit.action in ("strengthen", "archive"):
        rule = current.get(edit.rule_id or "")
        if rule is None:
            return None, f"no rule {edit.rule_id}"
        if rule.state == "pinned":
            return None, "the rule is pinned"
    if edit.action in ("add", "strengthen") and not supports:
        return None, "cites no lesson that supports it"
    return edit.model_copy(update={"supports": supports, "contradicts": contradicts}), ""


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def apply_edits(previous: Playbook | None, edits: list[RuleEdit], lessons: dict[str, Lesson],
                mandate: str, today: str) -> tuple[Playbook, dict[str, str]]:
    rules = [replace(r, supports=list(r.supports), contradicts=list(r.contradicts))
             for r in (previous.rules if previous else [])]
    by_id = {r.id: r for r in rules}
    next_n = 1 + max((int(r.id[1:]) for r in rules if r.id[1:].isdigit()), default=0)
    for e in edits:
        same = next((r for r in rules if _norm(r.text) == _norm(e.text)), None)
        if e.action == "add" and same is None:
            rid = f"r{next_n}"
            next_n += 1
            rules.append(Rule(rid, e.text.strip(), e.why.strip(), e.supports, e.contradicts))
            by_id[rid] = rules[-1]
            continue
        target = same if e.action == "add" else by_id[e.rule_id]
        if e.action == "archive":
            target.state = "archived"
            continue
        target.supports = list(dict.fromkeys(target.supports + e.supports))
        target.contradicts = list(dict.fromkeys(target.contradicts + e.contradicts))
    notes = {}
    for r in rules:
        if r.state in ("pinned", "archived", "promoted"):
            continue
        ok, why = gate.assess(r, lessons)
        r.state = "eligible" if ok else "candidate"
        notes[r.id] = why
    cited = {i for r in rules for i in r.supports + r.contradicts if i in lessons}
    available = max((lessons[i].known_on for i in cited), default=None)
    pb = Playbook(mandate, (previous.version + 1) if previous else 1, today, available, rules, gate.GATE_VERSION)
    return pb, notes


def _default_llm(config: dict):
    from tradingagents.llm_clients import create_llm_client

    return create_llm_client(provider=config["llm_provider"], model=config["deep_think_llm"],
                             base_url=config.get("backend_url")).get_llm()


def distill(config: dict, mandate: str, llm=None, today: str | None = None, dry_run: bool = False) -> DistillResult:
    today = today or date.today().isoformat()
    lessons = load_lessons(config, mandate=mandate, known_by=today)
    if not lessons:
        return DistillResult(None)
    current = latest(config, mandate)
    shown = lessons[-MAX_LESSONS:]
    proposal: Proposal = (llm or _default_llm(config)).with_structured_output(Proposal).invoke(
        _prompt(mandate, shown, current))
    by_id = {x.id: x for x in lessons}
    existing = {r.id: r for r in (current.rules if current else [])}
    accepted, rejected = [], []
    for e in proposal.edits:
        kept, why = check(e, by_id, existing)
        (accepted.append(kept) if kept else rejected.append((e, why)))
    pb, notes = apply_edits(current, accepted, by_id, mandate, today)
    if not dry_run and (accepted or current is None):
        save(config, pb)
    return DistillResult(pb, accepted, rejected, notes)
