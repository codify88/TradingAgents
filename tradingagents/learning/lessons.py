"""Lessons: what settled decisions and interim reviews taught, dated by when known.

One reader over every decision log (live and each sweep), shared by the
playbook distill here and, later, the knowledge layer's ``Lesson`` cards. The
rule is the one ``get_past_context`` already enforces: a lesson exists only
from the day its outcome was known -- the resolution date of a settled decision,
or of an interim review -- never from the analysis date.

Design: docs/design/hermes.md, part 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tradingagents.agents.utils.memory import TradingMemoryLog


@dataclass(frozen=True)
class Lesson:
    id: str             # ticker|date|mandate|horizon -- stable, citable
    mandate: str
    ticker: str
    date: str           # analysis date
    rating: str
    horizon_days: int
    alpha: str          # as logged, e.g. "-42.5%"
    known_on: str       # resolution date: when the outcome became known
    final: bool         # the settled outcome, or an interim review
    text: str           # the reflection or review note


def _logs(config: dict) -> list[Path]:
    results = Path(config["results_dir"])
    paths = sorted(results.glob("backtest/*/trading_memory.md"))
    live = Path(config.get("memory_log_path") or "")
    return paths + ([live] if live.name and live.exists() else [])


def load_lessons(config: dict, mandate: str | None = None, known_by: str | None = None) -> list[Lesson]:
    """Every lesson, oldest outcome first; only ``mandate``'s, and only those known by ``known_by``."""
    out: dict[str, Lesson] = {}
    for path in _logs(config):
        for e in TradingMemoryLog({"memory_log_path": str(path)}).load_entries():
            if e.get("superseded") or (mandate is not None and (e.get("mandate") or "") != mandate):
                continue
            base = f"{e['ticker']}|{e['date']}|{e.get('mandate') or ''}"
            for r in e.get("reviews") or []:
                if r.get("note"):
                    lid = f"{base}|{r['days']}d"
                    out[lid] = Lesson(lid, e.get("mandate") or "", e["ticker"], e["date"], e["rating"],
                                      int(r["days"]), r["alpha"], r["resolved"], False, r["note"].strip())
            holding = str(e.get("holding") or "").rstrip("d")
            if not e.get("pending") and e.get("resolved") and e.get("reflection") and holding.isdigit():
                lid = f"{base}|{holding}d"
                out[lid] = Lesson(lid, e.get("mandate") or "", e["ticker"], e["date"], e["rating"],
                                  int(holding), e.get("alpha") or "", e["resolved"], True,
                                  e["reflection"].strip())
    lessons = [x for x in out.values() if known_by is None or x.known_on <= known_by]
    return sorted(lessons, key=lambda x: (x.known_on, x.id))
