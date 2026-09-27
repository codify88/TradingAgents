"""Desk's research API: any symbol's data from every tool, and agent runs outside the book.

Reads are open like the rest of Desk's reads. A data fetch takes the action
token and runs in a server thread: it makes vendor requests but no model calls,
and finishes in seconds. An agent run spends API credits, so it takes the
estimate the operator confirmed, and runs as a background job (``ops.jobs``):
refused in the night window, but not by other jobs, because a research run
writes only under ``research/<SYMBOL>/`` and never touches the book or the
sweep logs.
"""

from __future__ import annotations

import re
import threading
from datetime import date

from fastapi import Request
from fastapi.responses import JSONResponse

DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def register(app, config: dict, authorised, refused, body_of, jobs_start=None, fetcher=None) -> None:
    from . import research

    fetching: set[tuple[str, str]] = set()
    lock = threading.Lock()

    def start_job(kind: str, args: list[str]) -> str:
        from tradingagents.ops import jobs

        if jobs_start:
            return jobs_start(config, kind, args)
        return jobs.start(config, kind, args, busy=lambda: None)

    def symbol_or_none(symbol: str) -> str | None:
        try:
            return research.clean_symbol(symbol)
        except ValueError:
            return None

    def day_of(b: dict) -> str | None:
        day = str(b.get("date") or date.today().isoformat())
        return day if DAY.match(day) and day <= date.today().isoformat() else None

    @app.get("/api/v1/research/search")
    def research_search(q: str = ""):
        return {"results": research.search(config, q)}

    @app.get("/api/v1/research/meta")
    def research_meta():
        from tradingagents.agentlab.runs import PRICES

        return {"groups": [{"key": k, "title": t} for k, t in research.GROUPS],
                "mandates": [m for m in research.MANDATES if m], "models": sorted(PRICES),
                "default_models": {"deep": config.get("deep_think_llm"), "quick": config.get("quick_think_llm")},
                "indices": [{"symbol": s, "name": n} for s, n in research.INDICES.items()]}

    @app.get("/api/v1/research/{symbol}")
    def research_overview(symbol: str):
        s = symbol_or_none(symbol)
        if s is None:
            return refused(400, "not a symbol")
        out = research.overview(config, s)
        out["fetching"] = sorted(d for sym, d in fetching if sym == s)
        return out

    @app.get("/api/v1/research/{symbol}/data/{day}")
    def research_data(symbol: str, day: str):
        s = symbol_or_none(symbol)
        if s is None or not DAY.match(day):
            return refused(400, "a symbol and a date")
        data = research.load_fetch(config, s, day)
        if data is None:
            return JSONResponse({"error": "not fetched"}, status_code=404)
        return data

    @app.post("/api/v1/research/{symbol}/fetch")
    async def research_fetch(symbol: str, request: Request):
        if not authorised(request):
            return refused(403, "not authorised")
        s = symbol_or_none(symbol)
        day = day_of(await body_of(request))
        if s is None or day is None:
            return refused(400, "a symbol and a date that isn't in the future")
        key = (s, day)
        with lock:
            if key in fetching:
                return {"result": f"{s} on {day} is already being fetched", "date": day}
            fetching.add(key)

        def work():
            try:
                (fetcher or research.fetch)(config, s, day)
            except Exception:  # the file records what finished; a crash leaves it "running"
                pass
            finally:
                with lock:
                    fetching.discard(key)

        threading.Thread(target=work, name=f"research-fetch-{s}", daemon=True).start()
        return {"result": f"Fetching everything for {s} as of {day}", "date": day}

    @app.post("/api/v1/research/estimate")
    async def research_estimate(request: Request):
        b = await body_of(request)
        return {**research.estimate(config, b.get("models") or {})}

    @app.post("/api/v1/research/{symbol}/runs")
    async def research_run(symbol: str, request: Request):
        if not authorised(request):
            return refused(403, "not authorised")
        b = await body_of(request)
        s, day = symbol_or_none(symbol), day_of(b)
        if s is None or day is None:
            return refused(400, "a symbol and a date that isn't in the future")
        mandate = str(b.get("mandate") or "")
        if mandate not in research.MANDATES:
            return refused(400, f"unknown mandate {mandate!r}")
        models = {k: str(v) for k, v in (b.get("models") or {}).items() if k in ("deep", "quick") and v}
        est = research.estimate(config, models)
        if est["cost_high"] is None:
            return refused(400, "no price known for these models; the run cannot be estimated")
        confirmed = b.get("confirm_cost")
        if not isinstance(confirmed, (int, float)) or confirmed + 0.01 < est["cost_high"]:
            return refused(400, f"confirm the estimate first (up to ${est['cost_high']:.2f})")
        rid = research.start_run(config, s, day, mandate, models)
        result = start_job(f"research {s} {mandate or 'no mandate'}", ["research", "execute", s, rid])
        if result.startswith("Refused"):
            research.discard_run(config, s, rid)
        return {"run": rid, "result": result}

    @app.get("/api/v1/research/{symbol}/runs/{run_id}")
    def research_report(symbol: str, run_id: str):
        s = symbol_or_none(symbol)
        got = research.report(config, s, run_id) if s else None
        if got is None:
            return JSONResponse({"error": "no such run"}, status_code=404)
        return got

    @app.get("/api/v1/research/{symbol}/decisions")
    def research_decisions(symbol: str):
        """The book's own decisions on this symbol, newest first."""
        s = symbol_or_none(symbol)
        if s is None:
            return refused(400, "not a symbol")
        return {"decisions": research.decisions(s)}

    @app.get("/api/v1/research/{symbol}/decisions/{day}")
    def research_decision_report(symbol: str, day: str):
        from tradingagents.mcp_server import tools

        s = symbol_or_none(symbol)
        if s is None or not DAY.match(day):
            return refused(400, "a symbol and a date")
        return {"text": tools.get_report(s, day)}
