"""Replays: the last stages of saved decisions, re-run under a variant.

A full decision is ~20 model calls and ~$1-2. The rating habit lives at the
end of the graph -- the research manager, the trader, the risk debate and the
portfolio manager -- and every backtest cell saves the state that fed those
stages (``full_states_log_<date>.json``: the analysts' reports, both debates,
the plans). A replay rebuilds that state, runs only the chosen tail under the
variant's edits and settings, and records the new rating beside the original.

Stages (each includes everything after it):

- ``pm``: the Portfolio Manager alone, on the saved risk debate (1 deep call);
- ``risk``: the three risk analysts, then the PM (3 quick + 1 deep);
- ``trader``: the trader, the risk debate, the PM;
- ``research``: the bull/bear debate, research manager, trader, risk, PM.

What differs from the original run, so compare a variant against a
``baseline`` replay of the same cases, never against the original ratings: the
lessons from past decisions (``past_context``) are not replayed, the
instrument line is the ticker-only form, and one sampling is one sampling.

Every case is scored on one horizon from the stored price panel -- the open
after the decision date to the open ``horizon`` sessions later, less SPY over
the same window -- whatever horizon its mandate was graded on.
"""

from __future__ import annotations

import json
import math
import random
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .variants import Variant, from_json

STAGES = {
    "pm": ["Portfolio Manager"],
    "risk": ["Aggressive Analyst", "Conservative Analyst", "Neutral Analyst", "Portfolio Manager"],
    "trader": ["Trader", "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst", "Portfolio Manager"],
    "research": ["Bull Researcher", "Bear Researcher", "Research Manager", "Trader", "Aggressive Analyst",
                 "Conservative Analyst", "Neutral Analyst", "Portfolio Manager"],
}
STAGE_LABELS = {"pm": "Portfolio Manager only", "risk": "Risk debate + PM", "trader": "Trader onward",
                "research": "Research debate onward"}
# (input, output) tokens per case, until a replay of that stage has measured them.
TOKENS = {"pm": (14_000, 2_000), "risk": (60_000, 8_000), "trader": (70_000, 9_000), "research": (120_000, 18_000)}
DEEP_SHARE = {"pm": 1.0, "risk": 0.3, "trader": 0.3, "research": 0.35}
BULLISH = ("Buy", "Overweight")
BEARISH = ("Underweight", "Sell")
PROB = re.compile(r"probability of beating the benchmark\W{0,6}(\d{1,3})\s*%", re.I)
DEFAULT_HORIZON = 5
MAX_CASES = 300


@dataclass
class Case:
    id: str                 # <run>/<ticker>/<date>
    run: str
    ticker: str
    date: str
    mandate: str
    rating: str             # the original run's rating
    path: str


def _root(config: dict) -> Path:
    d = Path(config["data_cache_dir"]) / "agentlab" / "replays"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- cases --------------------------------------------------------------------


def all_cases(config: dict) -> list[Case]:
    """Every saved decision state under ``results_dir/backtest``, with its mandate and rating."""
    from tradingagents.agents.utils.memory import TradingMemoryLog
    from tradingagents.agents.utils.rating import parse_rating

    base = Path(config["results_dir"]) / "backtest"
    out = []
    for run_dir in sorted(p for p in base.glob("*") if p.is_dir()):
        entries = {}
        log = run_dir / "trading_memory.md"
        if log.exists():
            for e in TradingMemoryLog({"memory_log_path": str(log)}).load_entries():
                entries[(e["ticker"], e["date"])] = e
        for f in sorted(run_dir.glob("*/TradingAgentsStrategy_logs/full_states_log_*.json")):
            ticker, day = f.parent.parent.name, f.stem.removeprefix("full_states_log_")
            e = entries.get((ticker, day))
            if e is None:     # no log entry: the run's mandate is unknown, so the prompts can't be rebuilt
                continue
            rating = e.get("rating") or ""
            if not rating:
                try:
                    rating = parse_rating(json.loads(f.read_text()).get("final_trade_decision", ""))
                except (OSError, ValueError):
                    continue
            out.append(Case(f"{run_dir.name}/{ticker}/{day}", run_dir.name, ticker, day,
                            e.get("mandate") or "", rating, str(f)))
    return out


def case_sets(config: dict) -> dict:
    """Counts by mandate and original rating, for choosing what to replay."""
    cs = all_cases(config)
    by: dict[str, dict[str, int]] = {}
    for c in cs:
        m = by.setdefault(c.mandate or "none", {})
        m[c.rating] = m.get(c.rating, 0) + 1
    return {"total": len(cs), "by_mandate": by}


def choose(config: dict, mandate: str = "any", count: int = 40, seed: int = 7,
           ids: list[str] | None = None) -> list[Case]:
    """``count`` cases, drawn at random (reproducibly) from one mandate or all."""
    cs = all_cases(config)
    if ids:
        want = set(ids)
        return [c for c in cs if c.id in want]
    if mandate != "any":
        cs = [c for c in cs if (c.mandate or "none") == mandate]
    rnd = random.Random(seed)
    rnd.shuffle(cs)
    return sorted(cs[: max(1, min(count, MAX_CASES))], key=lambda c: c.id)


# --- outcomes -----------------------------------------------------------------


def outcomes(config: dict, cases: list[Case], horizon: int = DEFAULT_HORIZON, panel=None) -> dict[str, float]:
    """Alpha vs SPY over ``horizon`` sessions from the open after each decision date."""
    if panel is None:
        from tradingagents.lab.panel import load_panel

        panel = load_panel(config)
    if panel is None or "SPY" not in panel.open.columns:
        return {}
    import pandas as pd

    dates = panel.open.index
    out = {}
    for c in cases:
        if c.ticker not in panel.open.columns:
            continue
        i = dates.searchsorted(pd.Timestamp(c.date), side="right")   # the first open after the decision
        j = i + horizon
        if j >= len(dates):
            continue
        s0, s1 = panel.open[c.ticker].iloc[i], panel.open[c.ticker].iloc[j]
        b0, b1 = panel.open["SPY"].iloc[i], panel.open["SPY"].iloc[j]
        if any(pd.isna(x) or x <= 0 for x in (s0, s1, b0, b1)):
            continue
        out[c.id] = float(s1 / s0 - b1 / b0)
    return out


# --- running ------------------------------------------------------------------


def estimate(config: dict, variant: Variant, stage: str, n: int) -> dict:
    from .runs import cost_range, price

    if stage not in STAGES:
        raise ValueError(f"stage must be one of {', '.join(STAGES)}")
    cfg = {**config, **variant.config_overrides()}
    deep, quick = cfg.get("deep_think_llm", ""), cfg.get("quick_think_llm", "")
    ti, to = measured(config, stage) or TOKENS[stage]
    if price(deep) is None or price(quick) is None:
        return {"stage": stage, "cases": n, "models": sorted({deep, quick}), "cost": None, "minutes": 0}
    share = DEEP_SHARE[stage]
    cost = 0.0
    for model, part in ((deep, share), (quick, 1 - share)):
        if part:
            rng = cost_range(int(ti * n * part), int(to * n * part), [model])
            cost += rng[1] if rng else 0
    minutes = {"pm": 0.6, "risk": 2.0, "trader": 2.3, "research": 4.5}[stage] * n
    return {"stage": stage, "cases": n, "models": sorted({deep, quick}), "cost": round(cost, 2),
            "minutes": math.ceil(minutes)}


def measured(config: dict, stage: str) -> tuple[int, int] | None:
    """Mean tokens per case from finished replays of this stage."""
    ins, outs = [], []
    for rid in all_replays(config):
        try:
            meta = json.loads((_root(config) / rid / "run.json").read_text())
        except (OSError, ValueError):
            continue
        if meta.get("stage") != stage:
            continue
        for r in _results(config, rid):
            if r.get("status") == "ok" and r.get("tokens_in"):
                ins.append(r["tokens_in"])
                outs.append(r["tokens_out"])
    return (int(sum(ins) / len(ins)), int(sum(outs) / len(outs))) if len(ins) >= 3 else None


def start(config: dict, variant: Variant, stage: str, cases: list[Case], horizon: int = DEFAULT_HORIZON) -> str:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {', '.join(STAGES)}")
    if not cases:
        raise ValueError("no cases to replay")
    rid = f"{variant.name}--{stage}--{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    d = _root(config) / rid
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({
        "id": rid, "variant": asdict(variant), "stage": stage, "horizon": horizon,
        "cases": [asdict(c) for c in cases], "status": "running",
        "started": datetime.now(UTC).isoformat(timespec="seconds")}, indent=2))
    return rid


def _results(config: dict, rid: str) -> list[dict]:
    try:
        return [json.loads(x) for x in (_root(config) / rid / "results.jsonl").read_text().splitlines() if x.strip()]
    except OSError:
        return []


def _state(saved: dict, stage: str, case: Case, mandate_context: str, risk_rounds: int) -> dict:
    """The graph state at the start of ``stage``, rebuilt from a saved decision."""
    inv = saved.get("investment_debate_state") or {}
    risk = saved.get("risk_debate_state") or {}
    state = {
        "messages": [], "company_of_interest": case.ticker, "asset_type": "stock", "instrument_context": "",
        "mandate": case.mandate, "mandate_context": mandate_context, "trade_date": case.date, "sender": "",
        "past_context": "", "portfolio_context": "",
        "market_report": saved.get("market_report", ""), "sentiment_report": saved.get("sentiment_report", ""),
        "news_report": saved.get("news_report", ""), "fundamentals_report": saved.get("fundamentals_report", ""),
        "mandate_reports": dict(saved.get("mandate_reports") or {}),
        "investment_debate_state": {"bull_history": "", "bear_history": "", "history": "", "current_response": "",
                                    "judge_decision": "", "count": 0},
        "risk_debate_state": {"aggressive_history": "", "conservative_history": "", "neutral_history": "",
                              "history": "", "latest_speaker": "", "current_aggressive_response": "",
                              "current_conservative_response": "", "current_neutral_response": "",
                              "judge_decision": "", "count": 0},
        "investment_plan": "", "trader_investment_plan": "", "final_trade_decision": "",
    }
    if stage in ("trader", "risk", "pm"):
        state["investment_debate_state"] = {**state["investment_debate_state"],
                                            **{k: inv.get(k, "") for k in ("bull_history", "bear_history", "history",
                                                                           "current_response", "judge_decision")}}
        state["investment_plan"] = saved.get("investment_plan", "")
    if stage in ("risk", "pm"):
        state["trader_investment_plan"] = saved.get("trader_investment_decision", "")
    if stage == "pm":
        state["risk_debate_state"] = {**state["risk_debate_state"],
                                      **{k: risk.get(k, "") for k in ("aggressive_history", "conservative_history",
                                                                      "neutral_history", "history")},
                                      "latest_speaker": "Neutral", "count": 3 * risk_rounds}
    return state


def _tail(stage: str, graph) -> object:
    """A graph of just the stage's nodes, wired as the full graph wires them, under the same names
    (the variant's edits are matched to agents by node name)."""
    from langgraph.graph import END, START, StateGraph

    from tradingagents.agents import (
        create_aggressive_debator,
        create_bear_researcher,
        create_bull_researcher,
        create_conservative_debator,
        create_neutral_debator,
        create_portfolio_manager,
        create_research_manager,
        create_trader,
    )
    from tradingagents.agents.utils.agent_states import AgentState
    from tradingagents.graph.setup import DEBATE_PATH_MAP, RISK_ANALYSIS_PATH_MAP

    deep, quick, logic = graph.deep_thinking_llm, graph.quick_thinking_llm, graph.conditional_logic
    factories = {
        "Bull Researcher": lambda: create_bull_researcher(quick),
        "Bear Researcher": lambda: create_bear_researcher(quick),
        "Research Manager": lambda: create_research_manager(deep),
        "Trader": lambda: create_trader(quick),
        "Aggressive Analyst": lambda: create_aggressive_debator(quick),
        "Conservative Analyst": lambda: create_conservative_debator(quick),
        "Neutral Analyst": lambda: create_neutral_debator(quick),
        "Portfolio Manager": lambda: create_portfolio_manager(deep),
    }
    nodes = STAGES[stage]
    w = StateGraph(AgentState)
    for n in nodes:
        w.add_node(n, factories[n]())
    w.add_edge(START, nodes[0])
    if "Bull Researcher" in nodes:
        for n in ("Bull Researcher", "Bear Researcher"):
            w.add_conditional_edges(n, logic.should_continue_debate, DEBATE_PATH_MAP)
        w.add_edge("Research Manager", "Trader")
    if "Trader" in nodes:
        w.add_edge("Trader", "Aggressive Analyst")
    if "Aggressive Analyst" in nodes:
        for n in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"):
            w.add_conditional_edges(n, logic.should_continue_risk_analysis, RISK_ANALYSIS_PATH_MAP)
    w.add_edge("Portfolio Manager", END)
    return w.compile()


def execute(config: dict, rid: str, graph_factory=None) -> dict:
    """Replay every case not yet done (resumable); one case's failure is not the run's."""
    from tradingagents.agents.utils.rating import parse_rating
    from tradingagents.ops.credits import is_out_of_credits
    from tradingagents.usage import UsageTracker

    from . import hook
    from .runs import run_config

    d = _root(config) / rid
    meta = json.loads((d / "run.json").read_text())
    variant = from_json(meta["variant"])
    stage = meta["stage"]
    cfg = run_config(config, variant, d)
    done = {r["case"] for r in _results(config, rid) if r.get("status") == "ok"}
    tracker = UsageTracker()
    graphs: dict[str, object] = {}

    def graph_for(mandate: str):
        if mandate not in graphs:
            if graph_factory is not None:
                graphs[mandate] = graph_factory(cfg, mandate, tracker)
            else:
                from tradingagents.graph.trading_graph import TradingAgentsGraph

                graphs[mandate] = TradingAgentsGraph(config=cfg, mandate=mandate or None, callbacks=[tracker])
        return graphs[mandate]

    with hook.installed(variant, capture=d / "captures.jsonl"):
        for raw in meta["cases"]:
            case = Case(**raw)
            if case.id in done:
                continue
            before = tracker.snapshot()
            row = {"case": case.id, "ticker": case.ticker, "date": case.date, "mandate": case.mandate,
                   "original": case.rating}
            try:
                g = graph_for(case.mandate)
                state = _state(json.loads(Path(case.path).read_text()), stage, case, g.mandate_context,
                               int(cfg.get("max_risk_discuss_rounds", 1)))
                token = hook.CASE.set((case.ticker, case.date))
                try:
                    out = _tail(stage, g).invoke(state, {"recursion_limit": 60})
                finally:
                    hook.CASE.reset(token)
                text = out.get("final_trade_decision", "")
                prob = PROB.search(text)
                row.update(status="ok", rating=parse_rating(text),
                           research=parse_rating(out.get("investment_plan", "")) if stage == "research" else None,
                           probability=int(prob.group(1)) / 100 if prob else None, decision=text[:6000])
            except Exception as exc:  # recorded; the next case still runs
                row.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:400])
                if is_out_of_credits(exc):
                    row["error"] = "the Anthropic credit balance is too low"
            used = tracker.snapshot() - before
            row.update(tokens_in=used.tokens_in, tokens_out=used.tokens_out, cache_read=used.cache_read,
                       cache_write=used.cache_write)
            with (d / "results.jsonl").open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            if row.get("error") == "the Anthropic credit balance is too low":
                break
    meta.update(status="finished", finished=datetime.now(UTC).isoformat(timespec="seconds"))
    (d / "run.json").write_text(json.dumps(meta, indent=2))
    return meta


# --- reading ------------------------------------------------------------------


def all_replays(config: dict) -> list[str]:
    return sorted((p.name for p in _root(config).iterdir() if (p / "run.json").exists()),
                  key=lambda n: (_root(config) / n / "run.json").stat().st_mtime, reverse=True)


def _mean(xs):
    return sum(xs) / len(xs) if xs else math.nan


def _t(a, b):
    if len(a) < 2 or len(b) < 2:
        return math.nan
    va = sum((x - _mean(a)) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - _mean(b)) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return (_mean(a) - _mean(b)) / se if se else math.nan


def metrics(config: dict, rid: str, panel=None) -> dict:
    from .runs import cost_range

    d = _root(config) / rid
    meta = json.loads((d / "run.json").read_text())
    rows = _results(config, rid)
    ok = [r for r in rows if r.get("status") == "ok"]
    cases = [Case(**c) for c in meta["cases"]]
    alpha = outcomes(config, cases, meta.get("horizon", DEFAULT_HORIZON), panel=panel)
    ratings = ("Buy", "Overweight", "Hold", "Underweight", "Sell", "REVIEW")

    def mix(key):
        return {r: sum(x.get(key) == r for x in ok) for r in ratings}

    moves: dict[str, dict[str, int]] = {}
    for x in ok:
        m = moves.setdefault(x["original"], {})
        m[x["rating"]] = m.get(x["rating"], 0) + 1

    def edge(key):
        scored = [(x[key], alpha[x["case"]]) for x in ok if x["case"] in alpha]
        bull = [a for r, a in scored if r in BULLISH]
        rest = [a for r, a in scored if r not in BULLISH]
        calls = [(r, a) for r, a in scored if r in BULLISH + BEARISH]
        hits = [(r in BULLISH) == (a > 0) for r, a in calls]
        return {"scored": len(scored), "bullish_n": len(bull),
                "edge": _mean(bull) - _mean(rest) if bull and rest else math.nan, "t": _t(bull, rest),
                "hit_rate": sum(hits) / len(hits) if hits else math.nan, "calls": len(calls)}

    probs = [(x["probability"], alpha[x["case"]] > 0) for x in ok if x.get("probability") is not None and x["case"] in alpha]
    v = meta["variant"]
    models = sorted({v.get("models", {}).get("deep") or config.get("deep_think_llm", ""),
                     v.get("models", {}).get("quick") or config.get("quick_think_llm", "")})
    ti, to = sum(x.get("tokens_in", 0) for x in rows), sum(x.get("tokens_out", 0) for x in rows)
    rng = cost_range(ti, to, models, sum(x.get("cache_read", 0) for x in rows),
                     sum(x.get("cache_write", 0) for x in rows))
    return {
        "id": rid, "variant": v["name"], "version": v.get("version", 0), "knobs": v.get("knobs", {}),
        "stage": meta["stage"], "horizon": meta.get("horizon", DEFAULT_HORIZON), "status": meta.get("status"),
        "started": meta.get("started"), "finished": meta.get("finished"),
        "cases": len(cases), "done": len(ok), "failed": sum(r.get("status") == "failed" for r in rows),
        "errors": sorted({r["error"] for r in rows if r.get("error")})[:3],
        "ratings": mix("rating"), "original_ratings": mix("original"), "moves": moves,
        "bullish_share": sum(x["rating"] in BULLISH for x in ok) / len(ok) if ok else math.nan,
        "hold_share": sum(x["rating"] == "Hold" for x in ok) / len(ok) if ok else math.nan,
        "new": edge("rating"), "original": edge("original"),
        "probability": {"n": len(probs),
                        "brier": _mean([(p - (1.0 if hit else 0.0)) ** 2 for p, hit in probs]),
                        "mean": _mean([p for p, _ in probs])} if probs else None,
        "tokens_in": ti, "tokens_out": to, "cost": rng[1] if rng else None,
        "per_case": (rng[1] / len(rows)) if rng and rows else None, "models": models,
        "case_ids": [c.id for c in cases],
    }


def decisions(config: dict, rid: str, limit: int = 300) -> list[dict]:
    """Each replayed case: original and new rating, outcome, and the new decision's text."""
    d = _root(config) / rid
    meta = json.loads((d / "run.json").read_text())
    alpha = outcomes(config, [Case(**c) for c in meta["cases"]], meta.get("horizon", DEFAULT_HORIZON))
    out = []
    for r in _results(config, rid)[:limit]:
        out.append({**{k: r.get(k) for k in ("case", "ticker", "date", "mandate", "original", "rating", "research",
                                             "probability", "status", "error", "decision")},
                    "alpha": alpha.get(r["case"])})
    return out
