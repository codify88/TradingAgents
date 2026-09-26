"""Desk's Today screen as data: what needs the operator, most urgent first.

One call returns everything the phone's first screen draws -- the pending
order plan with its cutoff, the trading switches, last night's run, reviews
coming due and the lab's open suggestions -- plus an ordered inbox of the items
that want attention. Every piece comes from the same functions the CLI, the
08:00 report and Hermes use, so the screens cannot disagree with them.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path

# Inbox severities, most urgent first. "critical" is a failure or a block,
# "action" something with a deadline, "attention" something to look at this
# week, "info" nothing to do.
SEVERITY = ("critical", "action", "attention", "info")


def _plan(config: dict, now: datetime | None) -> dict | None:
    from tradingagents.trading import plan as pl

    try:
        p = pl.latest_pending(config, now)
    except (ValueError, TypeError, KeyError):
        return None
    if p is None or not p.orders:
        return None
    now = (now or datetime.now(pl.NEW_YORK)).astimezone(pl.NEW_YORK)
    expires = datetime.fromisoformat(p.expires)
    return {**asdict(p), "seconds_left": max(0, int((expires - now).total_seconds())),
            "buys": len(p.buys), "sells": len(p.sells)}


def _night(config: dict) -> dict | None:
    from tradingagents.ops import nightly_log

    path = nightly_log.latest(Path(config["results_dir"]) / "nightly")
    if path is None:
        return None
    try:
        run = nightly_log.parse(path)
    except (OSError, ValueError):
        return None
    problems = list(run.problems())
    missed = nightly_log.missed_today(path, datetime.now())
    if missed:
        problems.insert(0, missed)
    return {"started": run.started.isoformat(), "finished": run.finished.isoformat() if run.finished else None,
            "exit_code": run.exit_code, "names_run": run.names_run, "failures": len(run.failures),
            "problems": problems, "summary": run.summary()}


def _reviews(within_days: int = 7) -> list[dict]:
    from tradingagents.mcp_server import tools

    try:
        return [{**i, "when": i["when"].isoformat()} for i in tools.review_items(within_days)]
    except Exception:  # a broken log must not blank the screen
        return []


def today(config: dict, now: datetime | None = None) -> dict:
    from tradingagents.lab.report import open_suggestions
    from tradingagents.trading.book import load_book

    book = load_book(config)
    plan = _plan(config, now)
    night = _night(config)
    reviews = _reviews()
    try:
        suggestions = open_suggestions(config)
    except (OSError, ValueError, KeyError):
        suggestions = []

    inbox: list[dict] = []
    if book.halted:
        inbox.append({"kind": "halted", "severity": "critical", "title": "Trading is halted",
                      "detail": book.halted_reason or "no reason given"})
    if book.blocked:
        inbox.append({"kind": "blocked", "severity": "critical",
                      "title": f"{len(book.blocked)} reconciliation mismatch(es)",
                      "detail": book.blocked[0]})
    if night and (night["problems"] or night["failures"]):
        inbox.append({"kind": "night", "severity": "critical", "title": "Last night needs a look",
                      "detail": "; ".join(night["problems"]) or f"{night['failures']} failed cell(s)"})
    if plan:
        inbox.append({"kind": "plan", "severity": "action", "title": f"Order plan · {plan['strategy']}",
                      "detail": f"{plan['buys']} buys, {plan['sells']} sells for the {plan['session']} open",
                      "plan_id": plan["id"]})
    overdue = [r for r in reviews if r["overdue"]]
    if reviews:
        inbox.append({"kind": "reviews", "severity": "attention" if overdue else "info",
                      "title": f"{len(reviews)} review(s) due this week",
                      "detail": " · ".join(f"{r['ticker']} ({r['days']}d)" for r in reviews[:4])})
    for s in suggestions:
        inbox.append({"kind": "suggestion", "severity": "attention",
                      "title": f"Lab suggestion · {s['strategy_key']}",
                      "detail": f"adopt {s['candidate']}", "strategy": s["strategy_key"]})
    if night and not (night["problems"] or night["failures"]):
        inbox.append({"kind": "night", "severity": "info",
                      "title": f"Night: {night['names_run']} decision(s), no failures",
                      "detail": night["summary"].splitlines()[0] if night["summary"] else ""})
    inbox.sort(key=lambda i: SEVERITY.index(i["severity"]))
    return {
        "plan": plan,
        "switches": {"halted": book.halted, "halted_reason": book.halted_reason,
                     "blocked": book.blocked, "refusal": book.refusal()},
        "night": night, "reviews": reviews, "suggestions": suggestions, "inbox": inbox,
        "needs_you": sum(i["severity"] in ("critical", "action") for i in inbox),
    }
