"""Shadow picks: what a candidate variant would pick tonight, recorded, never traded.

The lab can only test a variant on the past, and the past has been looked at.
"Buy last week's losers when the market is stressed" was found after its
2020-on weeks had been seen, so its test-period result is not clean evidence.
The only clean evidence is weeks that had not happened when the variant was
named. So each night, after the market state is refreshed, this records what
each shadow variant picks from that day's close -- through ``replay.select``,
the very code the lab replays -- beside the live screen's own ordering for
comparison. No trades, no model calls, no requests.

Records live in ``lab/shadow.jsonl``, one per (variant, date). A record is
scored once its holding has traded in the price panel: picks against the rest
of the eligible pool, and against the S&P 500 after a round-trip cost.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from .panel import lab_dir
from .replay import COST_PER_SIDE, STRATEGIES, Context, newey_west_t, select, variants

# Named 2026-09-26, before any forward week existed: the candidate, and the live
# standard screen's default ordering to compare it with.
SHADOW = {
    "standard": ("liquidity/p8/dv5m/if_stressed:reversal_5d", "liquidity/p8/dv5m"),
}


def _path(config: dict):
    return lab_dir(config) / "shadow.jsonl"


def records(config: dict) -> list[dict]:
    try:
        return [json.loads(x) for x in _path(config).read_text().splitlines() if x.strip()]
    except OSError:
        return []


def record(config: dict, ctx: Context, strategy: str = "standard") -> list[dict]:
    """Record each shadow variant's picks from the panel's last close; a date
    already recorded (a weekend night re-reading Friday's close) is skipped."""
    spec = STRATEGIES[strategy]
    i = len(ctx.dates) - 1
    day = ctx.dates[i].strftime("%Y-%m-%d")
    have = {(r["variant"], r["date"]) for r in records(config)}
    out = []
    for vid in SHADOW.get(strategy, ()):
        if (vid, day) in have:
            continue
        v = next((x for x in variants(spec) if x.id == vid), None)
        if v is None:
            continue  # a shadow variant the lab no longer defines is not recorded under a new meaning
        sel = select(ctx, spec, v, i, need_next_open=False)
        if sel is None:
            continue
        out.append({
            "recorded": datetime.now(UTC).isoformat(timespec="seconds"), "strategy": strategy,
            "variant": vid, "date": day, "signal": sel.signal, "cash": sel.cash,
            "condition": sel.condition,
            "picks": [str(s) for s in ctx.symbols[sel.chosen]],
            "pool": [str(s) for s in ctx.symbols[sel.pool]],
        })
    if out:
        with _path(config).open("a") as fh:
            for r in out:
                fh.write(json.dumps(r) + "\n")
    return out


@dataclass
class Scored:
    variant: str
    n: int
    pending: int
    selection: float          # mean picks - pool, a holding
    t: float
    net_vs_market: float      # picks after a round-trip cost, minus the S&P 500
    condition_days: int       # records where the variant's market-state condition held
    selection_when_held: float


def score(config: dict, ctx: Context, strategy: str = "standard") -> list[Scored]:
    horizon = STRATEGIES[strategy].horizon
    spy = ctx.col.get("SPY")
    index = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(ctx.dates)}
    out = []
    for vid in SHADOW.get(strategy, ()):
        rows, pending = [], 0
        for r in records(config):
            if r["variant"] != vid or r["strategy"] != strategy:
                continue
            i = index.get(r["date"])
            if i is None or i + 1 + horizon >= len(ctx.dates):
                pending += 1
                continue
            cols = lambda syms: np.array([ctx.col[s] for s in syms if s in ctx.col], dtype=int)  # noqa: E731
            fwd_p = ctx.forward(i, horizon, cols(r["picks"]))
            fwd_c = ctx.forward(i, horizon, cols(r["pool"]))
            picks = 0.0 if r["cash"] else float(np.nanmean(fwd_p)) if fwd_p.size else math.nan
            pool = float(np.nanmean(fwd_c)) if fwd_c.size else math.nan
            market = float(ctx.forward(i, horizon, np.array([spy]))[0]) if spy is not None else math.nan
            rows.append((picks - pool, picks - 2 * COST_PER_SIDE - market, bool(r.get("condition"))))
        sel = np.array([x[0] for x in rows]) if rows else np.array([])
        held = [x[0] for x in rows if x[2]]
        out.append(Scored(
            variant=vid, n=len(rows), pending=pending,
            selection=float(np.nanmean(sel)) if rows else math.nan,
            # Nightly records overlap (a 5-day hold recorded every session).
            t=newey_west_t(sel, horizon - 1) if rows else math.nan,
            net_vs_market=float(np.nanmean([x[1] for x in rows])) if rows else math.nan,
            condition_days=len(held),
            selection_when_held=float(np.mean(held)) if held else math.nan))
    return out


def _pct(x: float) -> str:
    return "—" if x is None or math.isnan(x) else f"{x:+.2%}"


def render(scored: list[Scored], latest: list[dict]) -> str:
    lines = ["# Shadow picks: recorded forward, never traded", "",
             "Named 2026-09-26, before any of these weeks existed; scored once each 5-day holding has traded.", ""]
    for r in latest:
        state = "stressed: " if r.get("condition") else ""
        lines.append(f"- {r['date']} {r['variant']}: {state}{r['signal']} -> {', '.join(r['picks']) or 'cash'}")
    if latest:
        lines.append("")
    lines += ["| Variant | Scored | Pending | Picks − pool | t | Net vs S&P | Stressed days | Picks − pool when stressed |",
              "|---|---|---|---|---|---|---|---|"]
    for s in scored:
        t = "—" if math.isnan(s.t) else f"{s.t:.2f}"
        lines.append(f"| {s.variant} | {s.n} | {s.pending} | {_pct(s.selection)} | {t} | {_pct(s.net_vs_market)} "
                     f"| {s.condition_days} | {_pct(s.selection_when_held)} |")
    lines += ["", "The variant differs from the live ordering only on stressed days, so evidence builds only "
              "when the market is stressed. Expect months before there is anything to read."]
    return "\n".join(lines)
