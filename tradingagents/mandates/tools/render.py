"""Shared rendering for mandate tool output.

Every mandate tool returns cited markdown in the same shape, so an analyst
reads the same structures whichever mandate it belongs to. Kept here rather
than in one mandate's tool module so the next mandate does not import another's
internals.
"""

from __future__ import annotations

import functools
import math


def table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def screens_block(screens, mandate: str) -> str:
    """The screen verdicts, with the vocabulary spelled out for the reader.

    The legend is repeated in every report on purpose: it is what stops a WATCH
    being read as a failure, or a NO DATA being quietly filled in from memory.
    """
    lines = [f"### Screens ({mandate} hard disqualifiers the data can settle)"]
    for s in screens:
        lines.append(f"- **{s.status}** -- {s.name}. {s.evidence}.")
    lines.append(
        "TRIPPED means the numbers meet the disqualifier. WATCH means they meet it "
        "on one reading but not another -- say which reading you believe and why. "
        "NO DATA means the data cannot settle it; say so, do not guess. Report every "
        "status exactly as given: a CLEAR or TRIPPED is settled by the numbers, and a "
        "different reading of your own may sit beside it but never replaces it."
    )
    return "\n".join(lines)


def unavailable(tool_name: str, ticker: str, err: Exception) -> str:
    return (
        f"UNAVAILABLE: {tool_name} could not load data for {ticker} "
        f"({type(err).__name__}: {err}). Report this gap explicitly in your analysis. "
        f"Do not substitute figures from memory or general knowledge."
    )


def guarded(fn):
    """Turn any data failure into an UNAVAILABLE notice instead of a graph crash."""
    @functools.wraps(fn)
    def wrapper(ticker: str, curr_date: str) -> str:
        try:
            return fn(ticker, curr_date)
        except Exception as e:  # vendor, network, parse: all reported, none fatal
            return unavailable(fn.__name__, ticker, e)
    return wrapper


def pct(x, digits: int = 1, signed: bool = True) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x * 100:{'+' if signed else ''}.{digits}f}%"


def num(x, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.{digits}f}"


def flag(x) -> str:
    return "n/a" if x is None else ("yes" if x else "no")
