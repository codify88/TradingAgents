"""A variant run on a suite, in its own directory, and what it measured.

A run is its own world: its own results directory and decision memory (a
test's decisions never teach the production agents), the variant's models,
settings and vendors, and the hook applying its edits and capturing prompts.
The variant as saved is copied into the run, so a later edit cannot change
what a run is said to have tested.

Metrics, per run, from the decisions it logged:

- the rating mix, and how often the stated time horizon matched the holding period;
- **agents**: the mean 5-day alpha of names rated Buy/Overweight minus the rest --
  whether the ratings sort names at all;
- direction hit rate: bullish ratings on names that beat the benchmark,
  bearish on names that lagged (Hold not counted);
- picks vs controls: the screen's own edge on these cases, the same for every
  variant on a suite;
- tokens, calls and cost; each edit's applied and missed counts.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from . import suites as su
from .variants import Variant

BULLISH, BEARISH = ("Buy", "Overweight"), ("Underweight", "Sell")
RATINGS = ("Buy", "Overweight", "Hold", "Underweight", "Sell")

# USD per million tokens (input, output), platform.claude.com pricing, 2026-09-26.
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-fable-5-1": (10.0, 50.0),
}
# Measured per decision (2026-09-26 runs): tokens in, tokens out.
TOKENS_PER_DECISION = (160_000, 40_000)


def price(model: str) -> tuple[float, float] | None:
    return next((p for k, p in sorted(PRICES.items(), key=lambda kv: -len(kv[0])) if model.startswith(k)), None)


def cost_range(tokens_in: int, tokens_out: int, models: list[str]) -> tuple[float, float] | None:
    """(low, high) cost; exact when every agent used one model."""
    ps = [price(m) for m in models]
    if not ps or any(p is None for p in ps):
        return None
    costs = [(tokens_in * pi + tokens_out * po) / 1e6 for pi, po in ps]
    return (min(costs), max(costs))


def _root(config: dict) -> Path:
    d = Path(config["data_cache_dir"]) / "agentlab" / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_config(config: dict, variant: Variant, run_dir: Path) -> dict:
    cfg = {**config, **variant.config_overrides(), "results_dir": str(run_dir),
           "memory_log_path": str(run_dir / "memory" / "trading_memory.md")}
    if variant.vendors:
        cfg["data_vendors"] = {**(config.get("data_vendors") or {}), **variant.vendors}
    return cfg


def estimate(config: dict, variant: Variant, suite: str) -> dict:
    s = next((x for x in su.all_suites(config) if x.name == suite), None)
    if s is None:
        raise ValueError(f"no suite {suite!r}")
    if s.status != "ready":
        raise ValueError(f"suite {suite!r} is {s.status} ({s.done} of {s.total} dates); wait until it is ready")
    cfg = {**config, **variant.config_overrides()}
    models = sorted({cfg.get("deep_think_llm", ""), cfg.get("quick_think_llm", "")})
    ti, to = TOKENS_PER_DECISION
    rng = cost_range(ti * s.cases, to * s.cases, models)
    return {"suite": suite, "cases": s.cases, "dates": len(s.dates), "models": models,
            "cost_low": rng[0] if rng else None, "cost_high": rng[1] if rng else None,
            "minutes": s.cases * 8, "cutoff_warning": su.cutoff_warning(s, models)}


def start_record(config: dict, variant: Variant, suite: str) -> str:
    run_id = f"{variant.name}--{suite}--{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    d = _root(config) / run_id
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"id": run_id, "variant": asdict(variant), "suite": suite,
                                            "started": datetime.now(UTC).isoformat(timespec="seconds"),
                                            "status": "running"}, indent=2))
    return run_id


def execute(config: dict, run_id: str) -> dict:
    """Run every case of the suite under the variant; resumable (decided cases are skipped)."""
    from tradingagents.screener import run as screen_run

    from .hook import installed
    from .variants import from_json

    d = _root(config) / run_id
    meta = json.loads((d / "run.json").read_text())
    variant = from_json(meta["variant"])
    cfg = run_config(config, variant, d)
    failures = []
    with installed(variant, capture=d / "captures.jsonl") as state:
        for m in su.manifests(config, meta["suite"]):
            try:
                result = screen_run.run(m, cfg)
                failures += [list(f) for f in getattr(result, "failures", [])]
            except Exception as exc:  # one screen's failure is not the run's
                failures.append([m.run_id, m.as_of, f"{type(exc).__name__}: {exc}"[:300]])
        meta["edits"] = {"applied": dict(state.applied), "missed": dict(state.missed)}
    meta.update(status="finished", finished=datetime.now(UTC).isoformat(timespec="seconds"), failures=failures)
    (d / "run.json").write_text(json.dumps(meta, indent=2))
    return meta


def _pct(s) -> float:
    try:
        return float(str(s).rstrip("%")) / 100
    except (TypeError, ValueError):
        return math.nan


def _mean(xs: list[float]) -> float:
    xs = [x for x in xs if not math.isnan(x)]
    return sum(xs) / len(xs) if xs else math.nan


def _t(a: list[float], b: list[float]) -> float:
    a, b = [x for x in a if not math.isnan(x)], [x for x in b if not math.isnan(x)]
    if len(a) < 2 or len(b) < 2:
        return math.nan
    va = sum((x - _mean(a)) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - _mean(b)) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return (_mean(a) - _mean(b)) / se if se > 0 else math.nan


def metrics(config: dict, run_id: str) -> dict:
    from tradingagents.agents.utils.memory import TradingMemoryLog

    d = _root(config) / run_id
    meta = json.loads((d / "run.json").read_text())
    variant = meta["variant"]
    cfg = {**config, **(variant.get("settings") or {})}
    holding = int(cfg.get("holding_period_days", 5))
    picks = {s for m in su.manifests(config, meta["suite"]) for s in m.pick_symbols}
    entries = []
    for log in sorted(d.glob("backtest/*/trading_memory.md")):
        entries += [e for e in TradingMemoryLog({"memory_log_path": str(log)}).load_entries() if not e.get("superseded")]
    settled = [e for e in entries if not e.get("pending")]
    alpha = {(e["ticker"], e["date"]): _pct(e.get("alpha")) for e in settled}
    bull = [alpha[(e["ticker"], e["date"])] for e in settled if e["rating"] in BULLISH]
    rest = [alpha[(e["ticker"], e["date"])] for e in settled if e["rating"] not in BULLISH]
    calls = [(e["rating"], alpha[(e["ticker"], e["date"])]) for e in settled if e["rating"] != "Hold"]
    hits = [(r in BULLISH) == (a > 0) for r, a in calls if not math.isnan(a)]
    horizon = [re.search(r"\*\*Time Horizon\*\*:\s*([^\n]*)", e.get("decision") or "") for e in entries]
    stated = [h.group(1).strip() for h in horizon if h]
    matched = sum(bool(re.search(rf"\b{holding}\s*(trading\s*)?days?\b", s, re.I)) for s in stated)
    usage = []
    for f in sorted(d.glob("backtest/*/usage.jsonl")):
        usage += [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
    cells = [u for u in usage if u.get("kind") == "cell"]
    ti, to = sum(u.get("tokens_in", 0) for u in cells), sum(u.get("tokens_out", 0) for u in cells)
    rcfg = {**config, **Variant(**{k: v for k, v in variant.items() if k in ("name", "models", "settings")}).config_overrides()}
    models = sorted({rcfg.get("deep_think_llm", ""), rcfg.get("quick_think_llm", "")})
    rng = cost_range(ti, to, models)
    pick_a = [a for (t, _), a in alpha.items() if t in picks]
    ctrl_a = [a for (t, _), a in alpha.items() if t not in picks]
    return {
        "id": run_id, "variant": variant["name"], "version": variant.get("version", 0), "suite": meta["suite"],
        "status": meta.get("status"), "started": meta.get("started"), "finished": meta.get("finished"),
        "decided": len(entries), "settled": len(settled),
        "ratings": {r: sum(e["rating"] == r for e in entries) for r in RATINGS},
        "horizon_stated": len(stated), "horizon_matched": matched,
        "agents": _mean(bull) - _mean(rest) if bull and rest else math.nan,
        "agents_t": _t(bull, rest), "bullish_n": len(bull),
        "hit_rate": sum(hits) / len(hits) if hits else math.nan, "calls": len(hits),
        "picks_vs_controls": _mean(pick_a) - _mean(ctrl_a) if pick_a and ctrl_a else math.nan,
        "tokens_in": ti, "tokens_out": to, "llm_calls": sum(u.get("llm_calls", 0) for u in cells),
        "cost_low": rng[0] if rng else None, "cost_high": rng[1] if rng else None,
        "per_decision": (rng[0] / len(cells)) if rng and cells else None,
        "edits": meta.get("edits", {}), "failures": len(meta.get("failures", [])), "models": models,
    }


def all_runs(config: dict) -> list[str]:
    return sorted((p.name for p in _root(config).iterdir() if (p / "run.json").exists()), reverse=True)


def captures(config: dict, run_id: str | None = None, node: str | None = None, limit: int = 1) -> list[dict]:
    """Captured prompts, newest run first: what each agent was actually sent."""
    ids = [run_id] if run_id else all_runs(config)
    out = []
    for rid in ids:
        f = _root(config) / rid / "captures.jsonl"
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            row = json.loads(line)
            if node and row["node"] != node:
                continue
            out.append({**row, "run": rid})
            if len(out) >= limit:
                return out
    return out
