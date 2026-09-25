"""The evidence gate: a rule reaches ``eligible`` only on enough settled evidence.

Fixed before the first distill and not lowered to get a result (hermes.md).
A rule needs support from several distinct names and dates -- one company's
story told five times is one data point -- and more support than contradiction.
Only final (settled) outcomes count toward the bar; interim reviews may be
cited but prove nothing on their own.
"""

from __future__ import annotations

GATE_VERSION = "2026-09-25.1"
MIN_SUPPORT = 5
MIN_DISTINCT_TICKERS = 3
MIN_DISTINCT_DATES = 2
MAX_CONTRADICT_RATIO = 0.5   # contradicting final outcomes / supporting ones


def assess(rule, lessons_by_id: dict) -> tuple[bool, str]:
    """Whether ``rule`` passes the gate, and why not if it does not."""
    support = [lessons_by_id[i] for i in rule.supports if i in lessons_by_id and lessons_by_id[i].final]
    against = [lessons_by_id[i] for i in rule.contradicts if i in lessons_by_id and lessons_by_id[i].final]
    if len(support) < MIN_SUPPORT:
        return False, f"{len(support)} settled outcomes support it; needs {MIN_SUPPORT}"
    tickers, dates = {x.ticker for x in support}, {x.date for x in support}
    if len(tickers) < MIN_DISTINCT_TICKERS:
        return False, f"support comes from {len(tickers)} names; needs {MIN_DISTINCT_TICKERS}"
    if len(dates) < MIN_DISTINCT_DATES:
        return False, f"support comes from {len(dates)} analysis date(s); needs {MIN_DISTINCT_DATES}"
    if len(against) > MAX_CONTRADICT_RATIO * len(support):
        return False, f"{len(against)} settled outcomes contradict it against {len(support)} for"
    return True, "passes"
