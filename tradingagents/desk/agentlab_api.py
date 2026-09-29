"""Desk's agent-lab API: the catalog, variants, prompt previews, suites and runs.

Reads are open like the rest of Desk's reads. Saving a variant and building a
suite take the action token. Starting a run spends API credits, so it also
takes the estimate the operator confirmed, and refuses a run over the cost cap
(``agentlab_max_cost``, $50 by default) unless the request says so explicitly.
Runs and suite builds are background jobs (``ops.jobs``): refused in the night
window and while another job runs.
"""

from __future__ import annotations

import math
import re

from fastapi import Request
from fastapi.responses import JSONResponse

DEFAULT_MAX_COST = 50.0


def _clean(x):
    """NaN is not JSON."""
    if isinstance(x, float) and math.isnan(x):
        return None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    return x


def register(app, config: dict, authorised, refused, body_of, jobs_start=None) -> None:
    from dataclasses import asdict

    from tradingagents.agentlab import (
        catalog as cat,
        hook,
        knobs,
        replay,
        runs,
        suites,
        variants as va,
    )

    def start_job(kind: str, args: list[str], exclusive: bool = True) -> str:
        from tradingagents.ops import jobs

        if jobs_start:
            return jobs_start(config, kind, args)
        # Replays write only their own directory, so other jobs don't block them.
        return jobs.start(config, kind, args) if exclusive else jobs.start(config, kind, args, busy=lambda: None)

    @app.get("/api/v1/agents/catalog")
    def agents_catalog():
        return cat.catalog(config)

    @app.get("/api/v1/agents/variants")
    def agents_variants():
        return {"variants": [asdict(v) for v in va.all_variants(config)],
                "kinds": list(va.KINDS), "settings": list(va.SETTINGS), "analysts": list(va.ANALYST_KEYS),
                "models": sorted(runs.PRICES)}

    @app.get("/api/v1/agents/variants/{name}/history")
    def agents_history(name: str):
        return {"history": va.history(config, name)}

    @app.post("/api/v1/agents/variants")
    async def agents_save(request: Request):
        if not authorised(request):
            return refused(403, "not authorised")
        b = await body_of(request)
        try:
            v = va.from_json({k: b.get(k) for k in ("name", "description", "edits", "models", "settings",
                                                    "vendors", "extra_tools", "knobs") if b.get(k) is not None})
            known_tools = set(cat.all_tools())
            from tradingagents.dataflows.interface import VENDOR_LIST

            saved = va.save(config, v, known_tools=known_tools, known_vendors=set(VENDOR_LIST))
        except (ValueError, TypeError) as exc:
            return refused(400, str(exc))
        return asdict(saved)

    @app.post("/api/v1/agents/preview")
    async def agents_preview(request: Request):
        """The latest captured prompt for an agent, before and after a variant's edits."""
        b = await body_of(request)
        node = str(b.get("node", ""))
        if node not in cat.NODES:
            return refused(400, f"unknown agent {node!r}")
        try:
            v = va.from_json(b["variant"]) if isinstance(b.get("variant"), dict) else va.load(config, str(b.get("variant") or "baseline"))
        except (ValueError, TypeError, KeyError) as exc:
            return refused(400, str(exc))
        got = runs.captures(config, node=node, limit=1)
        if not got:
            return {"node": node, "capture": None,
                    "note": "No prompt captured for this agent yet: run any variant on a suite (one case is enough)."}
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        kinds = {"system": SystemMessage, "human": HumanMessage, "ai": AIMessage}
        before = [kinds.get(m["role"], HumanMessage)(content=m["text"]) for m in got[0]["messages"]]
        after, applied, missed = hook.apply_edits(before, node, v.edits)
        return {"node": node, "capture": {k: got[0][k] for k in ("run", "ticker", "date", "model", "at")},
                "before": [{"role": m.type, "text": m.content} for m in before],
                "after": [{"role": m.type, "text": m.content} for m in after],
                "applied": applied, "missed": missed}

    @app.get("/api/v1/agents/suites")
    def agents_suites():
        return {"suites": [asdict(s) for s in suites.all_suites(config)], "cutoffs": suites.MODEL_CUTOFFS,
                "seconds_per_date": suites.SECONDS_PER_DATE}

    @app.post("/api/v1/agents/suites")
    async def agents_suite_build(request: Request):
        if not authorised(request):
            return refused(403, "not authorised")
        b = await body_of(request)
        name, start, end = str(b.get("name", "")), str(b.get("start", "")), str(b.get("end", ""))
        try:
            count, picks, controls = int(b.get("count", 40)), int(b.get("picks", 8)), int(b.get("controls", 4))
        except (TypeError, ValueError):
            return refused(400, "count, picks and controls are whole numbers")
        if not suites.NAME.match(name) or not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) for d in (start, end)):
            return refused(400, "a name (lowercase, digits, dashes) and start/end dates (YYYY-MM-DD)")
        if not (1 <= count <= 100 and 1 <= picks <= 20 and 0 <= controls <= 20):
            return refused(400, "count 1-100, picks 1-20, controls 0-20")
        if suites.exists(config, name):
            s = next((x for x in suites.all_suites(config) if x.name == name), None)
            state = f"is being built ({s.done} of {s.total} dates)" if s and s.status == "building" else "exists"
            return refused(400, f"a suite named {name!r} {state}; choose another name")
        return {"result": start_job(f"agent-lab suite {name}", [
            "agents", "suite-build", name, "--start", start, "--end", end, "--count", str(count),
            "--picks", str(picks), "--controls", str(controls)])}

    @app.post("/api/v1/agents/estimate")
    async def agents_estimate(request: Request):
        b = await body_of(request)
        try:
            v = va.load(config, str(b.get("variant", "")))
            return {**runs.estimate(config, v, str(b.get("suite", ""))),
                    "max_cost": float(config.get("agentlab_max_cost", DEFAULT_MAX_COST))}
        except ValueError as exc:
            return refused(400, str(exc))

    @app.post("/api/v1/agents/runs")
    async def agents_run(request: Request):
        if not authorised(request):
            return refused(403, "not authorised")
        b = await body_of(request)
        try:
            v = va.load(config, str(b.get("variant", "")))
            est = runs.estimate(config, v, str(b.get("suite", "")))
        except ValueError as exc:
            return refused(400, str(exc))
        cap = float(config.get("agentlab_max_cost", DEFAULT_MAX_COST))
        high = est["cost_high"]
        if high is None:
            return refused(400, "no price known for these models; the run cannot be estimated")
        confirmed = b.get("confirm_cost")
        if not isinstance(confirmed, (int, float)) or confirmed + 0.01 < high:
            return refused(400, f"confirm the estimate first (up to ${high:.2f})")
        if high > cap and b.get("over_cap") is not True:
            return refused(400, f"about ${high:.0f} is over the ${cap:.0f} cap; confirm going over it explicitly")
        rid = runs.start_record(config, v, est["suite"])
        return {"run": rid, "result": start_job(f"agent-lab run {rid}", ["agents", "execute", rid])}

    @app.get("/api/v1/agents/runs")
    def agents_runs():
        out = []
        for rid in runs.all_runs(config)[:50]:
            try:
                out.append(_clean(runs.metrics(config, rid)))
            except (OSError, ValueError, KeyError):
                continue
        return {"runs": out}

    @app.get("/api/v1/agents/runs/{run_id}")
    def agents_run_detail(run_id: str):
        if run_id not in runs.all_runs(config):
            return JSONResponse({"error": "no such run"}, status_code=404)
        return _clean(runs.metrics(config, run_id))

    @app.get("/api/v1/agents/jobs")
    def agents_jobs():
        from tradingagents.ops import jobs

        return {"text": jobs.status(config, limit=8)}

    # --- knobs --------------------------------------------------------------------

    @app.get("/api/v1/agents/knobs")
    def agents_knobs():
        return {"knobs": knobs.catalog(), "production": knobs.PRODUCTION}

    @app.post("/api/v1/agents/knobs")
    async def agents_knobs_save(request: Request):
        """Compose a variant from knob choices and save it."""
        if not authorised(request):
            return refused(403, "not authorised")
        b = await body_of(request)
        try:
            v = knobs.compose(str(b.get("name", "")), dict(b.get("choices") or {}),
                              str(b.get("description") or ""), dict(b.get("models") or {}))
            saved = va.save(config, v)
        except (ValueError, TypeError) as exc:
            return refused(400, str(exc))
        return asdict(saved)

    # --- replays ------------------------------------------------------------------

    def replay_request(b: dict):
        v = va.load(config, str(b.get("variant", "")))
        stage = str(b.get("stage", "pm"))
        try:
            count, seed, horizon = int(b.get("count", 40)), int(b.get("seed", 7)), int(b.get("horizon", 5))
        except (TypeError, ValueError) as exc:
            raise ValueError("count, seed and horizon are whole numbers") from exc
        if not (1 <= count <= replay.MAX_CASES and 1 <= horizon <= 63):
            raise ValueError(f"count 1-{replay.MAX_CASES}, horizon 1-63 sessions")
        ids = b.get("case_ids") or None
        cases = replay.choose(config, str(b.get("mandate", "any")), count, seed, ids=ids)
        if not cases:
            raise ValueError("no saved decisions match")
        return v, stage, cases, horizon

    @app.get("/api/v1/agents/replay/cases")
    def agents_replay_cases():
        return {**replay.case_sets(config), "stages": replay.STAGE_LABELS, "max": replay.MAX_CASES}

    @app.post("/api/v1/agents/replay/estimate")
    async def agents_replay_estimate(request: Request):
        try:
            v, stage, cases, _ = replay_request(await body_of(request))
            return {**replay.estimate(config, v, stage, len(cases)),
                    "max_cost": float(config.get("agentlab_max_cost", DEFAULT_MAX_COST))}
        except ValueError as exc:
            return refused(400, str(exc))

    @app.post("/api/v1/agents/replays")
    async def agents_replay_start(request: Request):
        if not authorised(request):
            return refused(403, "not authorised")
        b = await body_of(request)
        try:
            v, stage, cases, horizon = replay_request(b)
            est = replay.estimate(config, v, stage, len(cases))
        except ValueError as exc:
            return refused(400, str(exc))
        if est["cost"] is None:
            return refused(400, "no price known for these models; the replay cannot be estimated")
        confirmed = b.get("confirm_cost")
        if not isinstance(confirmed, (int, float)) or confirmed + 0.01 < est["cost"]:
            return refused(400, f"confirm the estimate first (up to ${est['cost']:.2f})")
        cap = float(config.get("agentlab_max_cost", DEFAULT_MAX_COST))
        if est["cost"] > cap and b.get("over_cap") is not True:
            return refused(400, f"about ${est['cost']:.0f} is over the ${cap:.0f} cap; confirm going over it explicitly")
        rid = replay.start(config, v, stage, cases, horizon)
        result = start_job(f"agent-lab replay {rid}", ["agents", "replay-execute", rid], exclusive=False)
        return {"replay": rid, "result": result}

    @app.get("/api/v1/agents/replays")
    def agents_replays():
        from tradingagents.lab.panel import load_panel

        panel = load_panel(config)
        out = []
        for rid in replay.all_replays(config)[:60]:
            try:
                out.append(_clean(replay.metrics(config, rid, panel=panel)))
            except (OSError, ValueError, KeyError):
                continue
        return {"replays": out}

    @app.get("/api/v1/agents/replays/{rid}/decisions")
    def agents_replay_decisions(rid: str):
        if rid not in replay.all_replays(config):
            return JSONResponse({"error": "no such replay"}, status_code=404)
        return {"decisions": _clean(replay.decisions(config, rid))}

    @app.get("/api/v1/agents/playbook")
    def agents_playbook():
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "docs" / "design" / "rating-playbook.md"
        return {"text": path.read_text() if path.exists() else "The playbook is missing from docs/design."}
