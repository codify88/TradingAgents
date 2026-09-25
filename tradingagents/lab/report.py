"""The lab's trial count, its report, and the suggestion it makes.

Every variant ever replayed for a strategy counts toward that strategy's trial
total, and the in-sample bar rises with it (``replay.t_hurdle``). The one
candidate is chosen on the tuning dates alone; the test dates only confirm or
reject it, so looking at them cannot move the choice.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from .panel import lab_dir
from .replay import OOS_T, SIGNALS, STRATEGIES, Result, Strategy, t_hurdle

NULL_SIGNALS = ("random",)


def record_trials(config: dict, strategy: Strategy, results: list[Result]) -> int:
    """Append this run's variants; return the strategy's distinct variant count."""
    path = lab_dir(config) / "trials.jsonl"
    at = datetime.now(UTC).isoformat(timespec="seconds")
    with path.open("a") as fh:
        for r in results:
            fh.write(json.dumps({"at": at, "strategy": strategy.name, "variant": r.variant.id,
                                 "tune_t": _num(r.tune.t), "test_selection": _num(r.test.selection)}) + "\n")
    return trial_count(config, strategy)


def trial_count(config: dict, strategy: Strategy) -> int:
    path = lab_dir(config) / "trials.jsonl"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return 0
    return len({json.loads(x)["variant"] for x in lines
                if x.strip() and json.loads(x)["strategy"] == strategy.name})


def _num(x: float) -> float | None:
    return None if x is None or math.isnan(x) else round(float(x), 6)


@dataclass
class Verdict:
    strategy: str
    candidate: str | None
    trials: int
    hurdle: float
    passes: bool
    tradeable: bool           # also beats the S&P 500 after costs on the test dates
    reason: str


def verdict(strategy: Strategy, results: list[Result], trials: int) -> Verdict:
    hurdle = t_hurdle(trials)
    live = [r for r in results if r.variant.signal not in NULL_SIGNALS and not math.isnan(r.tune.t)]
    if not live:
        return Verdict(strategy.name, None, trials, hurdle, False, False, "no variant could be scored")
    best = max(live, key=lambda r: r.tune.t)
    v = best.variant.id
    if best.tune.t < hurdle:
        why = (f"best on the tuning dates is {v} (t {best.tune.t:.2f}), under the {hurdle:.2f} "
               f"the {trials} variants tried require")
        return Verdict(strategy.name, v, trials, hurdle, False, False, why)
    if not (best.test.selection > 0 and best.test.t >= OOS_T):
        why = (f"{v} clears the tuning bar (t {best.tune.t:.2f}) but not the test dates "
               f"(selection {_pct(best.test.selection)}, t {best.test.t:.2f}; needs > 0 and t >= {OOS_T})")
        return Verdict(strategy.name, v, trials, hurdle, False, False, why)
    tradeable = best.test.net_vs_market > 0
    why = (f"{v} clears both: tuning t {best.tune.t:.2f} >= {hurdle:.2f}, test selection "
           f"{_pct(best.test.selection)} (t {best.test.t:.2f}); after costs vs the S&P 500 "
           f"{_pct(best.test.net_vs_market)} a holding")
    return Verdict(strategy.name, v, trials, hurdle, True, tradeable, why)


def save_suggestion(config: dict, v: Verdict) -> None:
    if not v.passes:
        return
    path = lab_dir(config) / "suggestions.jsonl"
    with path.open("a") as fh:
        fh.write(json.dumps({"at": datetime.now(UTC).isoformat(timespec="seconds"), **asdict(v)}) + "\n")


def _pct(x: float) -> str:
    return "—" if x is None or math.isnan(x) else f"{x:+.2%}"


def _t(x: float) -> str:
    return "—" if math.isnan(x) else f"{x:.2f}"


def render(strategy: Strategy, results: list[Result], v: Verdict, split: str) -> str:
    lines = [f"# Screen lab: {strategy.name} ({strategy.horizon}-day hold, every {strategy.every})", ""]
    if strategy.note:
        lines += [strategy.note, ""]
    lines += [f"Tuning dates before {split}, test dates from {split}. Selection = picks minus the "
              f"eligible pool, a holding, before costs. Net = picks after a round-trip cost minus the "
              f"S&P 500. t is Newey-West.", "",
              "| Variant | Tune n | Tune sel | Tune t | Test n | Test sel | Test t | Hit | Test net | Net t |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(results, key=lambda r: -(r.tune.t if not math.isnan(r.tune.t) else -99)):
        lines.append(
            f"| {r.variant.id} | {r.tune.n} | {_pct(r.tune.selection)} | {_t(r.tune.t)} | {r.test.n} "
            f"| {_pct(r.test.selection)} | {_t(r.test.t)} | {r.test.hit:.0%} | {_pct(r.test.net_vs_market)} "
            f"| {_t(r.test.t_net)} |")
    any_r = next((r for r in results if r.test.n), None)
    if any_r:
        s = any_r.test
        lines += ["", f"Ladder on the test dates (same for every variant): style ({strategy.style} − S&P 500) "
                  f"{_pct(s.style)}, universe (pool − style) {_pct(s.universe)}, "
                  f"size (RSP − S&P 500) {_pct(s.size)} a holding."]
    nulls = [r for r in results if r.variant.signal in NULL_SIGNALS]
    if nulls:
        worst = max(abs(r.tune.selection) for r in nulls if not math.isnan(r.tune.selection))
        lines += [f"Null check: the random ordering's selection is at most {_pct(worst)} on the tuning "
                  f"dates; it should be near zero."]
    lines += ["", "## Verdict", "", f"Trials counted for {strategy.name}: {v.trials} "
              f"(in-sample hurdle t {v.hurdle:.2f}).", "",
              ("**Suggest:** " if v.passes else "**No change suggested:** ") + v.reason + "."]
    if v.passes and not v.tradeable:
        lines.append("It picks better than the pool but does not beat the S&P 500 after costs on the test "
                     "dates; useful as a screen, not yet as a trading rule on its own.")
    lines += ["", "Signals: " + "; ".join(f"`{k}` {d}" for k, d in SIGNALS.items()
                                         if any(r.variant.signal == k for r in results)) + "."]
    return "\n".join(lines)


def open_suggestions(config: dict) -> list[dict]:
    """The latest passing suggestion per strategy that is not already in force."""
    from .adopted import current

    path = lab_dir(config) / "suggestions.jsonl"
    try:
        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    except OSError:
        return []
    latest: dict[str, dict] = {}
    for r in rows:
        latest[r["strategy"]] = r
    out = []
    for key, r in latest.items():
        strategy = next((k for k, s in STRATEGIES.items() if s.name == key), key)
        live = current(config, strategy)
        if live and live["variant"] == r["candidate"]:
            continue
        out.append({**r, "strategy_key": strategy})
    return out


def render_suggestions(config: dict) -> str:
    rows = open_suggestions(config)
    if not rows:
        return "No open suggestions: nothing in the lab has cleared the bar that is not already in use."
    lines = []
    for r in rows:
        lines.append(f"{r['strategy_key']}: adopt {r['candidate']} -- {r['reason']}."
                     + ("" if r["tradeable"] else " (Beats the pool, not yet the S&P 500 after costs.)")
                     + f"\n  tradingagents lab adopt {r['strategy_key']} {r['candidate']}")
    return "\n".join(lines)
