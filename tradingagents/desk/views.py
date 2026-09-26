"""What Desk shows, as plain data over this system's files.

Every function takes the config and returns JSON-ready data; nothing here knows
about HTTP, so it tests like the rest of the repo. The text panels reuse the MCP
tools' answers (``mcp_server.tools``), so Desk, Hermes and the CLI cannot
disagree about what happened.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict

from tradingagents.lab import adopted, report
from tradingagents.lab.panel import lab_dir
from tradingagents.lab.replay import SIGNALS, STRATEGIES

BrokerFactory = Callable[[dict], object]


def lab(config: dict) -> dict:
    suggestions = {s["strategy_key"]: s for s in report.open_suggestions(config)}
    history = adopted.adoptions(config)
    out = {}
    for key, spec in STRATEGIES.items():
        default = adopted.DEFAULT_STANDARD.id if key == "standard" else None
        out[key] = {
            "name": spec.name, "horizon": spec.horizon, "every": spec.every, "style": spec.style,
            "note": spec.note, "reads_live": key in adopted.READ_LIVE, "default": default,
            "current": adopted.current(config, key),
            "history": [r for r in reversed(history) if r["strategy"] == key],
            "suggestion": suggestions.get(key),
            "results": report.load_results(config, key),
            "report": (lab_dir(config) / f"{key}-report.md").exists(),
        }
    return {"strategies": out, "signals": SIGNALS}


def lab_report(config: dict, key: str) -> str | None:
    if key not in STRATEGIES:
        return None
    path = lab_dir(config) / f"{key}-report.md"
    return path.read_text() if path.exists() else None


def adopt(config: dict, strategy: str, variant: str, reason: str) -> dict:
    if not reason.strip():
        raise ValueError("say why: the reason is part of the record")
    row = adopted.adopt(config, strategy, variant, reason.strip())
    return {"adopted": row, "reads_live": strategy in adopted.READ_LIVE}


def _broker(config: dict, factory: BrokerFactory | None):
    from tradingagents.trading.broker import AlpacaBroker, BrokerError

    try:
        return (factory or AlpacaBroker)(config), None
    except BrokerError as exc:
        return None, str(exc)


def trading(config: dict, broker_factory: BrokerFactory | None = None) -> dict:
    from tradingagents.trading import book as bk, plan as pl
    from tradingagents.trading.broker import BrokerError

    book = bk.load_book(config)
    try:
        plans = pl.plans(config)
        pending = pl.latest_pending(config)
        plan_error = None
    except (ValueError, TypeError, KeyError) as exc:  # a broken plan file must not blank the page
        plans, pending, plan_error = [], None, str(exc)
    broker, broker_error = _broker(config, broker_factory)
    account = None
    if broker is not None:
        try:
            account = {**asdict(broker.account()), "paper": broker.paper}
        except BrokerError as exc:
            broker_error = str(exc)
    return {
        "book": {**asdict(book), "live": [asdict(c) | {"shares": c.shares()} for c in book.live]},
        "refusal": book.refusal(),
        "pending": asdict(pending) if pending else None,
        "plans": [{"id": p.id, "status": p.status, "session": p.session, "decision_date": p.decision_date,
                   "orders": len(p.orders), "submitted": p.submitted} for p in reversed(plans[-15:])],
        "plan_error": plan_error,
        "events": list(reversed(bk.events(config, 40))),
        "broker": {"available": broker is not None, "error": broker_error, "account": account},
    }


def trade_action(config: dict, action: str, body: dict,
                 broker_factory: BrokerFactory | None = None) -> str:
    """One of the operator's trading actions; returns what it did, as text."""
    from tradingagents.trading import execute as ex

    if action == "halt":  # the kill switch works without the broker
        broker, _ = _broker(config, broker_factory)
        return ex.halt(config, broker, (body.get("reason") or "operator via Desk").strip())
    if action == "resume":
        return ex.resume(config)
    if action == "ack":
        return ex.acknowledge(config)
    broker, error = _broker(config, broker_factory)
    if broker is None:
        raise ValueError(f"the broker is unavailable: {error}")
    if action == "submit":
        plan_id = str(body.get("plan_id") or "")
        if not re.fullmatch(r"[\w-]+", plan_id):
            raise ValueError("which plan?")
        return ex.submit(config, broker, plan_id)
    if action == "reconcile":
        problems = ex.reconcile(config, broker)
        return ("Book and broker agree." if not problems else
                "Mismatches (new orders are blocked until acknowledged):\n" + "\n".join(problems))
    raise ValueError(f"unknown action {action!r}")


def overview() -> dict:
    from tradingagents.mcp_server import tools

    return {"morning": tools.morning_report(), "jobs": tools.job_status(),
            "suggestions": tools.suggestions()}


def decisions(ticker: str | None, mandate: str | None, since: str | None, limit: int) -> str:
    from tradingagents.mcp_server import tools

    return tools.list_decisions(ticker=ticker or None, mandate=mandate or None,
                                since=since or None, limit=limit)


def decision_report(ticker: str, day: str) -> str:
    from tradingagents.mcp_server import tools

    return tools.get_report(ticker, day)
