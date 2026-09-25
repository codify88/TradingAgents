"""The operator's watchers: short messages when something needs a look, silence otherwise.

Each returns text to deliver, or "" -- a Hermes ``--no-agent`` job delivers a
script's output verbatim and stays silent on empty output, so an ordinary day
sends nothing. No model writes these, so they cannot misreport.

- ``reviews``: interim and final reviews of open decisions due this week;
- ``edge``: the weekly picks-vs-controls verdict and each active trial's state;
- ``earnings``: open decisions whose company reports in the next seven days;
- ``ownership``: congressional and insider trades that became public in the last
  day, in names with an open decision.

Design: docs/design/hermes.md, part 1 (watchers).
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from tradingagents.agents.utils.memory import TradingMemoryLog


def _config() -> dict:
    from tradingagents.default_config import DEFAULT_CONFIG

    return DEFAULT_CONFIG


def open_positions(config: dict | None = None) -> set[str]:
    """Tickers with an open (pending) decision anywhere."""
    from tradingagents.learning.lessons import _logs

    names = set()
    for path in _logs(config or _config()):
        for e in TradingMemoryLog({"memory_log_path": str(path)}).load_entries():
            if e.get("pending") and not e.get("superseded"):
                names.add(e["ticker"])
    return names


def reviews(today: str | None = None) -> str:
    from tradingagents.mcp_server.tools import pending_reviews

    text = pending_reviews(7, today=today)
    return "" if text.startswith("No reviews due") else f"Reviews this week\n\n{text}"


def edge() -> str:
    from tradingagents.evaluation.bar import verdict
    from tradingagents.evaluation.compare import compare
    from tradingagents.evaluation.trials import load_trials, trials_in_style
    from tradingagents.mcp_server.tools import screen_review

    config = _config()
    review = screen_review()
    verdict_line = review.split("## Verdict", 1)[-1].strip().splitlines()
    first = verdict_line[0].replace("**", "") if verdict_line else "No screens yet."
    lines = ["Weekly edge report", "", first]
    for t in load_trials(config):
        if t.status != "active":
            continue
        comps = compare(t, config)
        v = verdict(comps, trials_in_style(config, t.style))
        lines.append(f"- trial {t.id}: {v.counted}/{v.required} screens counted, "
                     f"candidate ahead on {v.wins}; {v.outcome}")
    return "\n".join(lines)


def earnings(today: str | None = None, calendar_csv: str | None = None) -> str:
    names = open_positions()
    if not names:
        return ""
    if calendar_csv is None:
        from tradingagents.dataflows.alpha_vantage_common import _make_api_request

        calendar_csv = _make_api_request("EARNINGS_CALENDAR", {"horizon": "3month"})
    start = date.fromisoformat(today) if today else date.today()
    end = start + timedelta(days=7)
    rows = [r for r in csv.DictReader(io.StringIO(calendar_csv))
            if r.get("symbol") in names and start.isoformat() <= (r.get("reportDate") or "") <= end.isoformat()]
    if not rows:
        return ""
    rows.sort(key=lambda r: r["reportDate"])
    return "Earnings this week for names with an open decision\n\n" + "\n".join(
        f"- {r['reportDate']} {r['symbol']} (quarter ending {r.get('fiscalDateEnding')}, "
        f"estimate {r.get('estimate') or 'n/a'})" for r in rows)


def ownership(today: str | None = None) -> str:
    from tradingagents.datastore import get_store, store_mode

    names = open_positions()
    if not names or store_mode() == "off":
        return ""
    store = get_store()
    day = date.fromisoformat(today) if today else date.today()
    since = (day - timedelta(days=1)).isoformat()
    lines = []
    for t in sorted(names):
        for e in store.edges_to(f"ticker:{t}", "traded", day.isoformat()):
            if e["available_at"] < since:
                continue
            who = store.node(e["src"]) or {}
            if e["src"].startswith("politician:"):
                lines.append(f"- {t}: {who.get('name', e['src'])} ({who.get('party', '?')}) "
                             f"{e.get('type', '').lower()} {e.get('amount_min')}-{e.get('amount_max')}, "
                             f"traded {e['as_of']}, filed {e['available_at']}")
            else:
                side = {"A": "bought", "D": "sold"}.get(e.get("side"), e.get("side"))
                lines.append(f"- {t}: {who.get('name', e['src'])} ({e.get('title')}) {side} "
                             f"{e.get('shares')} on {e['as_of']}")
    return ("Insider and congressional trades made public in the last day, in names with an "
            "open decision\n\n" + "\n".join(lines)) if lines else ""
