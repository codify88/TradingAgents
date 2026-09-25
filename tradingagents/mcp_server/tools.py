"""What the operator can ask this system, as plain functions over its files.

The MCP server (``server.py``) registers these as tools; they import nothing
from MCP, so they test like any other function. Every tool here only reads.
Their parameters are the tool's whole input surface, so none takes a config or
a path: where to read is this process's own configuration, never the caller's.
None returns an environment variable or a key: the vendor keys stay in this
repo's ``.env``, and an agent reading these answers never sees them.

Design: docs/design/hermes.md, part 1.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.ops import nightly_log

# A report can run to tens of thousands of characters; a tool answer that large
# crowds out everything else in the reader's context.
MAX_REPORT_CHARS = 40_000

_REPORT_KEYS = (
    ("Market", "market_report"), ("Sentiment", "sentiment_report"), ("News", "news_report"),
    ("Fundamentals", "fundamentals_report"), ("Research manager", "investment_plan"),
    ("Trader", "trader_investment_decision"), ("Portfolio manager", "final_trade_decision"),
)


def _config() -> dict:
    from tradingagents.default_config import DEFAULT_CONFIG

    return DEFAULT_CONFIG


def _results() -> Path:
    return Path(_config()["results_dir"])


# -- the nightly run -----------------------------------------------------------------


def nightly_status(day: str | None = None) -> str:
    """How last night's run went: cells run and failed, exit code, model usage,
    store savings, and anything that needs attention. ``day`` is YYYY-MM-DD;
    default the latest run."""
    log_dir = _results() / "nightly"
    path = (nightly_log.log_for(log_dir, date.fromisoformat(day)) if day
            else nightly_log.latest(log_dir))
    if path is None:
        return f"No nightly run found{' for ' + day if day else ''} in {log_dir}."
    return nightly_log.parse(path).summary()


# -- decisions -----------------------------------------------------------------------


def _all_entries() -> list[dict]:
    config = _config()
    logs = sorted((_results() / "backtest").glob("*/trading_memory.md"))
    live = Path(config.get("memory_log_path") or "")
    entries = []
    for path in [*logs, live]:
        if not path.name or not path.exists():
            continue
        source = path.parent.name if path.parent.parent.name == "backtest" else "live"
        for e in TradingMemoryLog({"memory_log_path": str(path)}).load_entries():
            if not e.get("superseded"):
                entries.append({**e, "source": source})
    return entries


def list_decisions(ticker: str | None = None, mandate: str | None = None,
                   since: str | None = None, limit: int = 50) -> str:
    """Logged decisions, newest analysis date first: ticker, date, rating, and
    outcome (alpha vs the benchmark) once settled. Filter by ticker, mandate, or
    analysis date on or after ``since`` (YYYY-MM-DD)."""
    rows = _all_entries()
    if ticker:
        rows = [e for e in rows if e["ticker"].upper() == ticker.strip().upper()]
    if mandate:
        rows = [e for e in rows if (e.get("mandate") or "") == mandate]
    if since:
        rows = [e for e in rows if e["date"] >= since]
    rows.sort(key=lambda e: (e["date"], e["ticker"]), reverse=True)
    if not rows:
        return "No decisions match."
    lines = [f"{len(rows)} decision(s){' (showing ' + str(limit) + ')' if len(rows) > limit else ''}:"]
    for e in rows[:limit]:
        if e.get("pending"):
            reviews = e.get("reviews") or []
            outcome = (f"in progress, {len(reviews)} review(s), latest alpha {reviews[-1]['alpha']}"
                       if reviews else "in progress")
        else:
            outcome = f"alpha {e.get('alpha')} over {e.get('holding')}"
        lines.append(f"- {e['date']} {e['ticker']} {e['rating']} [{e.get('mandate') or 'no mandate'}] "
                     f"{outcome} ({e['source']})")
    return "\n".join(lines)


def _mandate_horizons(name: str) -> tuple[int, ...]:
    from tradingagents.mandates.registry import get_mandate

    m = get_mandate(name)
    if m is None:
        return ()
    return tuple(sorted({*m.review_horizons_days, m.horizon_days}))


def pending_reviews(within_days: int = 30, today: str | None = None) -> str:
    """Interim and final reviews of open decisions that fall due within
    ``within_days`` calendar days, plus any already overdue. A review is due
    once its horizon, in trading days after the analysis date, has passed."""
    now = pd.Timestamp(today or date.today())
    horizon_end = now + pd.Timedelta(days=within_days)
    due, overdue = [], []
    for e in _all_entries():
        if not e.get("pending"):
            continue
        done = {r["days"] for r in e.get("reviews") or []}
        for days in _mandate_horizons(e.get("mandate") or ""):
            if days in done:
                continue
            when = pd.Timestamp(e["date"]) + pd.offsets.BDay(days)
            item = (when.date(), e["ticker"], e["date"], days, e.get("mandate") or "")
            if when < now:
                overdue.append(item)
            elif when <= horizon_end:
                due.append(item)
            break  # only the next outstanding horizon matters
    if not due and not overdue:
        return f"No reviews due in the next {within_days} days."
    lines = []
    if overdue:
        lines.append(f"Overdue ({len(overdue)}): settled when that ticker next runs, or by a sweep.")
        lines += [f"- {w} {t} (decided {d}), {h}-day review [{m}]" for w, t, d, h, m in sorted(overdue)]
    if due:
        lines.append(f"Due in the next {within_days} days ({len(due)}):")
        lines += [f"- {w} {t} (decided {d}), {h}-day review [{m}]" for w, t, d, h, m in sorted(due)]
    return "\n".join(lines)


# -- reports -------------------------------------------------------------------------


def get_report(ticker: str, day: str) -> str:
    """A decision's reports for ``ticker`` on analysis date ``day``: each
    analyst, the research and portfolio managers, and the trader."""
    ticker = ticker.strip().upper()
    date.fromisoformat(day)  # refuse anything that is not a date before touching paths
    if not ticker.replace(".", "").replace("-", "").isalnum():
        return f"Not a ticker: {ticker!r}."
    name = f"full_states_log_{day}.json"
    results = _results()
    candidates = [results / ticker / "TradingAgentsStrategy_logs" / name,
                  *sorted((results / "backtest").glob(f"*/{ticker}/TradingAgentsStrategy_logs/{name}"))]
    found = [p for p in candidates if p.exists()]
    if not found:
        return f"No report for {ticker} on {day}."
    state = json.loads(found[-1].read_text(encoding="utf-8"))
    parts = [f"# {ticker} {day} ({found[-1].parent.parent.parent.name})"]
    for title, key in _REPORT_KEYS:
        text = state.get(key)
        if text:
            parts.append(f"## {title}\n{text}")
    for title, text in (state.get("mandate_reports") or {}).items():
        if text:
            parts.append(f"## {title}\n{text}")
    out = "\n\n".join(parts)
    if len(out) > MAX_REPORT_CHARS:
        out = out[:MAX_REPORT_CHARS] + f"\n\n[... cut at {MAX_REPORT_CHARS:,} characters]"
    return out


# -- screens and the store -------------------------------------------------------------


def screen_review(mandate: str | None = None) -> str:
    """Whether each screen's picks beat their random controls, once outcomes settle."""
    from tradingagents.screener.review import render_performance

    return render_performance(_config(), mandate, None)


def data_store_stats(day: str | None = None) -> str:
    """The point-in-time store's size, and the day's requests served vs fetched."""
    from datetime import UTC, datetime

    from tradingagents.datastore import get_store, store_mode
    from tradingagents.datastore.policy import NEW_YORK

    mode = store_mode()
    if mode == "off":
        return "The data store is off."
    store = get_store()
    s = store.stats()
    day = day or datetime.now(UTC).astimezone(NEW_YORK).date().isoformat()
    counts = store.counts(day)
    served = sum(c.get("hit", 0) for c in counts.values())
    fetched = sum(c.get("fetch", 0) for c in counts.values())
    asked = served + fetched
    lines = [f"Data store ({mode}): {s['bytes'] / 1e6:,.1f} MB, {s['responses']:,} responses "
             f"({s['final']:,} final), {s['snapshots']:,} snapshots."]
    if asked:
        lines.append(f"{day}: {asked:,} requests asked, {served:,} served from the store "
                     f"({served / asked:.0%}), {fetched:,} fetched.")
    else:
        lines.append(f"{day}: no requests recorded.")
    return "\n".join(lines)


READ_TOOLS = (nightly_status, screen_review, list_decisions, get_report,
              pending_reviews, data_store_stats)
