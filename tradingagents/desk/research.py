"""Research any symbol from Desk: every tool's data, the agents' view, or both.

- ``fetch``: runs every tool the agents can call -- the four analysts' tools,
  the harvested ownership and earnings-call tools, and the value, momentum and
  LEAPS mandate tools -- for one symbol as of one date, and saves each result
  with its vendor, duration and any error. No model calls. Sections are written
  as they finish, so Desk shows progress.
- ``run``: the full agent pipeline under a chosen mandate (or none), in the
  research folder with its own decision memory -- a research run never teaches
  the production agents or counts in the screens' statistics.

Everything lives under ``<results_dir>/research/<SYMBOL>/``.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SYMBOL = re.compile(r"^\^?[A-Z0-9][A-Z0-9.\-]{0,11}$")
INDICATORS = ("close_50_sma", "close_200_sma", "rsi", "macd", "atr", "boll")
MACRO = ("fed_funds_rate", "10y_treasury", "cpi", "unemployment")
# What applies to an index: no statements, filings, holders or options chain.
INDEX_SECTIONS = {"news", "global_news", "markets", "trend", "strength"}
MANDATES = ("", "equity_value", "equity_momentum", "equity_momentum_leaps")

GROUPS = [
    ("price", "Price and technicals"),
    ("fundamentals", "Fundamentals and statements"),
    ("calls", "Earnings calls"),
    ("news", "News and sentiment"),
    ("ownership", "Insiders, Congress and institutions"),
    ("macro", "Macro and events"),
    ("value", "Value mandate data"),
    ("momentum", "Momentum mandate data"),
    ("leaps", "LEAPS mandate data"),
]


def clean_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if not SYMBOL.match(s):
        raise ValueError(f"{symbol!r} is not a symbol (letters, digits, '.', '-', or ^ for an index)")
    return s


def _plan(symbol: str, day: str) -> list[tuple[str, str, str, str, dict]]:
    """(section key, group, title, tool, arguments) for every call ``fetch`` makes."""
    d = datetime.fromisoformat(day)
    ago = lambda n: (d - timedelta(days=n)).strftime("%Y-%m-%d")  # noqa: E731
    t = {"ticker": symbol, "curr_date": day}
    out = [
        ("snapshot", "price", "Verified market snapshot", "get_verified_market_snapshot",
         {"symbol": symbol, "curr_date": day, "look_back_days": 30}),
        ("prices", "price", "Daily prices, last 90 days", "get_stock_data",
         {"symbol": symbol, "start_date": ago(90), "end_date": day}),
        *[(f"ind_{i}", "price", f"Indicator: {i}", "get_indicators",
           {"symbol": symbol, "indicator": i, "curr_date": day, "look_back_days": 60}) for i in INDICATORS],
        ("overview", "fundamentals", "Company overview", "get_fundamentals", t),
        ("income", "fundamentals", "Income statement (quarterly)", "get_income_statement", {**t, "freq": "quarterly"}),
        ("balance", "fundamentals", "Balance sheet (quarterly)", "get_balance_sheet", {**t, "freq": "quarterly"}),
        ("cashflow", "fundamentals", "Cash flow (quarterly)", "get_cashflow", {**t, "freq": "quarterly"}),
        ("transcript", "calls", "Latest earnings call", "get_earnings_call", {"ticker": symbol, "trade_date": day}),
        ("news", "news", "Company news, last 7 days", "get_news", {"ticker": symbol, "start_date": ago(7), "end_date": day}),
        ("global_news", "news", "Global news, last 7 days", "get_global_news",
         {"curr_date": day, "look_back_days": 7, "limit": 20}),
        ("insiders", "ownership", "Insider transactions", "get_insider_transactions", {"ticker": symbol, "trade_date": day}),
        ("congress", "ownership", "Congressional trades", "get_congress_trades", {"ticker": symbol, "trade_date": day}),
        ("holders", "ownership", "Institutional holders (13F)", "get_institutional_holdings", {"ticker": symbol, "trade_date": day}),
        ("etfs", "ownership", "ETFs that hold it", "get_etf_exposure", {"ticker": symbol, "trade_date": day}),
        *[(f"macro_{m}", "macro", f"Macro: {m.replace('_', ' ')}", "get_macro_indicators",
           {"indicator": m, "curr_date": day, "look_back_days": 365}) for m in MACRO],
        ("markets", "macro", f"Prediction markets: {symbol}", "get_prediction_markets", {"topic": symbol, "limit": 10}),
        ("quality", "value", "Quality metrics and screens", "get_quality_metrics", t),
        ("valuation", "value", "Valuation history", "get_valuation_history", t),
        ("reverse_dcf", "value", "Reverse DCF", "get_reverse_dcf", t),
        ("allocation", "value", "Capital allocation", "get_capital_allocation", t),
        ("trend", "momentum", "Trend structure", "get_trend_structure", t),
        ("strength", "momentum", "Relative strength", "get_relative_strength", t),
        ("growth", "momentum", "Growth trajectory", "get_growth_trajectory", t),
        ("revisions", "momentum", "Estimate revisions", "get_estimate_revisions", t),
        ("calendar", "momentum", "Earnings calendar", "get_earnings_calendar", t),
        ("leaps", "leaps", "LEAPS candidates", "get_leaps_candidates", t),
    ]
    if symbol.startswith("^"):
        out = [row for row in out if row[0] in INDEX_SECTIONS or row[1] in ("price", "macro")]
    return out


def all_research_tools() -> dict:
    """Every tool ``fetch`` may call: the agents' and the mandates'."""
    import importlib
    import pkgutil

    from langchain_core.tools import BaseTool

    import tradingagents.mandates as mandates
    from tradingagents.agentlab.catalog import all_tools

    tools = dict(all_tools())
    for m in pkgutil.walk_packages(mandates.__path__, mandates.__name__ + "."):
        try:
            mod = importlib.import_module(m.name)
        except Exception:  # a mandate module that cannot import contributes nothing
            continue
        for obj in vars(mod).values():
            if isinstance(obj, BaseTool):
                tools.setdefault(obj.name, obj)
    return tools


@dataclass
class Section:
    key: str
    group: str
    title: str
    tool: str
    args: dict
    vendor: str | None = None
    status: str = "pending"      # pending | ok | empty | unavailable | error
    text: str = ""
    seconds: float = 0.0


def _dir(config: dict, symbol: str) -> Path:
    d = Path(config["results_dir"]) / "research" / symbol
    d.mkdir(parents=True, exist_ok=True)
    return d


MANDATE_GROUPS = ("value", "momentum", "leaps")


def _vendor(tool: str, group: str, config: dict) -> str | None:
    """The configured vendor; the mandate tools read Alpha Vantage statements and prices."""
    from tradingagents.agentlab.catalog import _vendor as configured

    return configured(tool, config) or ("alpha_vantage" if group in MANDATE_GROUPS else None)


UNAVAILABLE = re.compile(r"^(DATA_UNAVAILABLE|NO_DATA_AVAILABLE|UNAVAILABLE\b|Optional .{0,40}unavailable|<.{0,80}unavailable"
                         r"|.{0,40}not configured)", re.I)
FAILED = re.compile(r"^(Error|Failed|Exception|Traceback)", re.I)
EMPTY = re.compile(r"^(No |Nothing |none\b)", re.I)


def status_of(text: str) -> str:
    """ok, empty (the source has nothing), unavailable (no key, vendor or data) or error.

    Tools often put a heading above their "unavailable" line, so the check reads
    the first line that isn't a heading.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "empty"
    body = next((ln for ln in lines if not ln.startswith("#")), lines[0])
    if FAILED.match(body):
        return "error"
    if UNAVAILABLE.match(body):
        return "unavailable"
    if EMPTY.match(body) and len(text) < 600:
        return "empty"
    return "ok"


def _save(path: Path, meta: dict, sections: list[Section]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**meta, "sections": [asdict(s) for s in sections]}))
    tmp.replace(path)


def fetch(config: dict, symbol: str, day: str, tools: dict | None = None, workers: int = 6,
          invoke: Callable | None = None) -> Path:
    """Every tool for ``symbol`` on ``day``; returns the saved file."""
    from tradingagents.dataflows.config import set_config

    symbol = clean_symbol(symbol)
    datetime.fromisoformat(day)
    set_config(config)
    tools = tools if tools is not None else all_research_tools()
    sections = [Section(k, g, title, tool, args, _vendor(tool, g, config)) for k, g, title, tool, args in _plan(symbol, day)]
    sections = [s for s in sections if s.tool in tools]
    path = _dir(config, symbol) / f"data-{day}.json"
    meta = {"symbol": symbol, "date": day, "started": datetime.now(UTC).isoformat(timespec="seconds"),
            "status": "running", "groups": GROUPS}
    _save(path, meta, sections)

    def call(s: Section) -> Section:
        t0 = time.monotonic()
        try:
            out = invoke(s.tool, s.args) if invoke else tools[s.tool].invoke(s.args)
            text = out if isinstance(out, str) else json.dumps(out, default=str, indent=1)
            s.text = text
            s.status = status_of(text)
        except Exception as exc:  # one tool's failure is not the fetch's
            s.status, s.text = "error", f"{type(exc).__name__}: {exc}"[:2000]
        s.seconds = round(time.monotonic() - t0, 1)
        return s

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(call, s) for s in sections]
        for _ in as_completed(futures):
            _save(path, meta, sections)
    meta.update(status="finished", finished=datetime.now(UTC).isoformat(timespec="seconds"))
    _save(path, meta, sections)
    return path


def load_fetch(config: dict, symbol: str, day: str) -> dict | None:
    path = _dir(config, clean_symbol(symbol)) / f"data-{day}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# --- agent runs ----------------------------------------------------------------------


def estimate(config: dict, models: dict | None = None) -> dict:
    """One decision's cost and time with these models (the agent lab's per-decision figures)."""
    from tradingagents.agentlab.runs import TOKENS_PER_DECISION, cost_range

    models = models or {}
    names = sorted({models.get("deep") or config.get("deep_think_llm", ""),
                    models.get("quick") or config.get("quick_think_llm", "")})
    rng = cost_range(*TOKENS_PER_DECISION, names)
    return {"models": names, "cost_low": rng[0] if rng else None, "cost_high": rng[1] if rng else None,
            "minutes": 8}


def run_id_for(symbol: str, day: str, mandate: str) -> str:
    return f"agents-{day}-{mandate or 'none'}-{datetime.now():%H%M%S}-{uuid.uuid4().hex[:4]}"


def start_run(config: dict, symbol: str, day: str, mandate: str, models: dict | None = None) -> str:
    symbol = clean_symbol(symbol)
    datetime.fromisoformat(day)
    if mandate not in MANDATES:
        raise ValueError(f"unknown mandate {mandate!r}")
    rid = run_id_for(symbol, day, mandate)
    d = _dir(config, symbol) / rid
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"id": rid, "symbol": symbol, "date": day, "mandate": mandate,
                                            "models": models or {}, "status": "running",
                                            "started": datetime.now(UTC).isoformat(timespec="seconds")}, indent=2))
    return rid


def execute_run(config: dict, symbol: str, rid: str, graph_factory: Callable | None = None) -> dict:
    """The pipeline for one research run; its report and rating saved beside it."""
    from tradingagents.reporting import write_report_tree

    symbol = clean_symbol(symbol)
    d = _dir(config, symbol) / rid
    meta = json.loads((d / "run.json").read_text())
    models = meta.get("models") or {}
    cfg = {**config, "results_dir": str(d), "memory_log_path": str(d / "memory" / "trading_memory.md"),
           "checkpoint_enabled": False}
    if models.get("deep"):
        cfg["deep_think_llm"] = models["deep"]
    if models.get("quick"):
        cfg["quick_think_llm"] = models["quick"]
    try:
        if graph_factory is None:
            from tradingagents.graph.trading_graph import TradingAgentsGraph

            graph = TradingAgentsGraph(config=cfg, mandate=meta["mandate"] or None)
        else:
            graph = graph_factory(cfg, meta["mandate"])
        final_state, rating = graph.propagate(symbol, meta["date"])
        write_report_tree(final_state, symbol, d / "report")
        meta.update(status="finished", rating=str(rating))
    except Exception as exc:
        meta.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
    meta["finished"] = datetime.now(UTC).isoformat(timespec="seconds")
    (d / "run.json").write_text(json.dumps(meta, indent=2))
    return meta


def discard_run(config: dict, symbol: str, rid: str) -> None:
    """Forget a run whose job was refused, so it doesn't sit "running" forever."""
    import shutil

    if re.fullmatch(r"agents-[\w-]+", rid):
        shutil.rmtree(_dir(config, clean_symbol(symbol)) / rid, ignore_errors=True)


def decisions(symbol: str) -> list[dict]:
    """The book's logged decisions on ``symbol``, newest first."""
    from tradingagents.mcp_server.tools import _all_entries

    rows = [e for e in _all_entries() if e["ticker"].upper() == symbol]
    rows.sort(key=lambda e: e["date"], reverse=True)
    keep = ("date", "rating", "mandate", "source", "pending", "alpha", "holding")
    return [{k: e.get(k) for k in keep} for e in rows[:50]]


def runs(config: dict, symbol: str) -> list[dict]:
    d = _dir(config, clean_symbol(symbol))
    out = []
    for p in sorted(d.glob("agents-*/run.json"), reverse=True):
        try:
            out.append(json.loads(p.read_text()))
        except ValueError:
            continue
    return out


def report(config: dict, symbol: str, rid: str) -> dict | None:
    if not re.fullmatch(r"agents-[\w-]+", rid):
        return None
    d = _dir(config, clean_symbol(symbol)) / rid
    if not (d / "run.json").exists():
        return None
    meta = json.loads((d / "run.json").read_text())
    complete = d / "report" / "complete_report.md"
    return {**meta, "report": complete.read_text() if complete.exists() else ""}


def overview(config: dict, symbol: str) -> dict:
    """What is on file for a symbol: data fetches, research runs, and past decisions."""
    symbol = clean_symbol(symbol)
    d = _dir(config, symbol)
    fetches = []
    for p in sorted(d.glob("data-*.json"), reverse=True):
        try:
            data = json.loads(p.read_text())
        except ValueError:
            continue
        counts = {k: sum(s["status"] == k for s in data["sections"]) for k in ("ok", "empty", "unavailable", "error", "pending")}
        fetches.append({"date": data["date"], "status": data["status"], "counts": counts})
    return {"symbol": symbol, "name": lookup(config, symbol), "fetches": fetches, "runs": runs(config, symbol)}


# --- symbols ----------------------------------------------------------------------


def _listing(config: dict) -> list[dict]:
    """Active US listings (stocks and ETFs) from the stored LISTING_STATUS -- no request."""
    import csv
    import io

    from tradingagents.datastore import get_store
    from tradingagents.datastore.store import params_key

    row = get_store(config).stored("alpha_vantage", "LISTING_STATUS", params_key({}))
    if not row:
        return []
    return [r for r in csv.DictReader(io.StringIO(row[0])) if r.get("status") == "Active"]


INDICES = {"^GSPC": "S&P 500 index", "^DJI": "Dow Jones Industrial Average", "^IXIC": "Nasdaq Composite",
           "^RUT": "Russell 2000", "^VIX": "CBOE Volatility Index"}


def lookup(config: dict, symbol: str) -> dict | None:
    if symbol in INDICES:
        return {"symbol": symbol, "name": INDICES[symbol], "type": "Index", "exchange": ""}
    return next(({"symbol": r["symbol"], "name": r.get("name", ""), "type": r.get("assetType", ""),
                  "exchange": r.get("exchange", "")} for r in _listing(config) if r.get("symbol") == symbol), None)


def search(config: dict, q: str, limit: int = 12) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    up, low = q.upper(), q.lower()
    hits = [{"symbol": s, "name": n, "type": "Index", "exchange": ""} for s, n in INDICES.items()
            if up in s or low in n.lower()]
    exact, prefix, named = [], [], []
    for r in _listing(config):
        sym, name = r.get("symbol", ""), r.get("name", "")
        item = {"symbol": sym, "name": name, "type": r.get("assetType", ""), "exchange": r.get("exchange", "")}
        if sym == up:
            exact.append(item)
        elif sym.startswith(up):
            prefix.append(item)
        elif len(q) >= 3 and low in name.lower():
            named.append(item)
    prefix.sort(key=lambda x: len(x["symbol"]))

    def rank(item: dict) -> tuple:
        name = item["name"].lower()
        where = 0 if name.startswith(low) else 1 if re.search(rf"\b{re.escape(low)}", name) else 2
        fund = item["type"] != "Stock" or bool(re.search(r"\b(etf|etn|etns|fund|trust|shares)\b", name))
        return (where, fund, len(name))

    named.sort(key=rank)
    return (exact + hits + prefix + named)[:limit]
