"""The review surface: what the screen did, and whether it is working.

Two questions, deliberately separated because they become answerable at very
different times:

- *What did it do?* Answerable immediately, from the manifest alone, with no
  LLM spend. This is how you sanity-check a screen before paying for tier 3.
- *Is it working?* Answerable only once outcomes settle, by joining the picks
  and the controls back to the decision log. The control group is the whole
  point: "the picks made money" says nothing without "and the controls did
  not". A screener that cannot beat a random draw from its own eligible pool is
  costing you money to add noise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from tradingagents.agents.utils.memory import TradingMemoryLog

from .manifest import ScreenManifest, load_manifests


def render_screen(manifest: ScreenManifest, excluded: dict[str, list[str]] | None = None) -> str:
    """The immediate report: funnel, shortlist, control, and what was dropped."""
    lines = [
        f"# Screen {manifest.run_id}",
        "",
        f"Mandate **{manifest.mandate or 'none'}** as of **{manifest.as_of}** "
        f"(run {manifest.created}).",
        "",
        "## Funnel",
        "",
        "| Tier | Examined | Kept | Dropped |",
        "|---|---|---|---|",
    ]
    for tier in manifest.tiers:
        lines.append(f"| {tier['name']} | {tier['examined']:,} | {tier['kept']:,} | {tier['dropped']:,} |")

    lines += ["", "## Why names were dropped", ""]
    for tier in manifest.tiers:
        if not tier.get("reasons"):
            continue
        lines.append(f"**{tier['name']}**")
        lines.append("")
        for reason, count in sorted(tier["reasons"].items(), key=lambda kv: -kv[1]):
            lines.append(f"- {count:,} — {reason}")
        lines.append("")

    lines += [
        "Exclusions are the mandate's own hard disqualifiers, run in reverse, and "
        "only where the data says **TRIPPED**. A WATCH is exactly the contestable "
        "call the analyst debate exists to make, and NO DATA is not evidence of "
        "anything, so neither excludes.",
        "",
        "## Shortlist",
        "",
        f"Ordered by: *{manifest.ordering_signal}*. This is the one place the "
        "screen expresses a preference; everything above it was exclusion. If "
        "the picks systematically beat the controls below, that ordering is "
        "earning its keep. If they do not, it is not.",
        "",
        "| Rank | Symbol | Ordering value |",
        "|---|---|---|",
    ]
    for pick in manifest.picks:
        lines.append(f"| {pick['rank']} | {pick['symbol']} | {_fmt(pick['value'])} |")

    lines += [
        "",
        "## Control group",
        "",
        f"Drawn at random (seed `{manifest.control_seed}`) from the "
        f"{manifest.eligible_count:,} names that cleared every exclusion but were "
        "*not* ranked into the shortlist. Run these through the loop too: they "
        "are the only way to tell whether the ordering adds anything over "
        "picking an eligible name at random.",
        "",
        "| Symbol | Ordering value |",
        "|---|---|",
    ]
    for control in manifest.controls:
        lines.append(f"| {control['symbol']} | {_fmt(control['value'])} |")

    if manifest.notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in manifest.notes]

    if excluded:
        lines += ["", f"## Excluded ({len(excluded):,} names)", "",
                  "<details><summary>Full exclusion list</summary>", ""]
        for symbol in sorted(excluded)[:400]:
            lines.append(f"- `{symbol}` — {'; '.join(excluded[symbol])}")
        if len(excluded) > 400:
            lines.append(f"- …and {len(excluded) - 400:,} more")
        lines += ["", "</details>"]
    return "\n".join(lines)


@dataclass
class GroupOutcome:
    """Settled results for one arm of a screen run."""

    label: str
    settled: int
    pending: int
    mean_alpha: float
    hit_rate: float          # share with positive alpha
    symbols: list[str]

    @property
    def measurable(self) -> bool:
        return self.settled > 0


def _alpha(entry: dict) -> float:
    raw = entry.get("alpha")
    if not raw:
        return float("nan")
    try:
        return float(str(raw).strip().rstrip("%")) / 100
    except ValueError:
        return float("nan")


def _group(label: str, symbols: list[str], entries: list[dict], as_of: str) -> GroupOutcome:
    alphas, pending = [], 0
    for symbol in symbols:
        matches = [e for e in entries
                   if e["ticker"] == symbol and e["date"] == as_of and not e.get("superseded")]
        if not matches:
            pending += 1
            continue
        entry = matches[-1]
        if entry.get("pending"):
            pending += 1
            continue
        value = _alpha(entry)
        if math.isnan(value):
            pending += 1
            continue
        alphas.append(value)
    return GroupOutcome(
        label=label,
        settled=len(alphas),
        pending=pending,
        mean_alpha=float(sum(alphas) / len(alphas)) if alphas else float("nan"),
        hit_rate=float(sum(a > 0 for a in alphas) / len(alphas)) if alphas else float("nan"),
        symbols=symbols,
    )


def score_manifest(manifest: ScreenManifest, log: TradingMemoryLog) -> tuple[GroupOutcome, GroupOutcome]:
    entries = log.load_entries()
    return (
        _group("picks", manifest.pick_symbols, entries, manifest.as_of),
        _group("control", manifest.control_symbols, entries, manifest.as_of),
    )


def render_performance(config: dict, mandate: str | None = None) -> str:
    """Picks against controls across every saved screen, once outcomes settle."""
    manifests = load_manifests(config, mandate)
    if not manifests:
        return "No screens have been run yet."

    log = TradingMemoryLog(config)
    lines = [
        "# Screen performance",
        "",
        "Each row compares the shortlist against the random control drawn from "
        "the same eligible pool on the same date. The control column is the "
        "benchmark that matters: beating the market while losing to your own "
        "control means the exclusions are working and the ordering is not.",
        "",
        "Screen ids are the time suffix of the manifest filename under "
        "`results_dir/screens/`.",
        "",
        "| Screen | Style | As of | Picks | Picks α | Control | Control α | Edge |",
        "|---|---|---|---|---|---|---|---|",
    ]
    totals = {"picks": [], "control": []}
    for manifest in manifests:
        picks, control = score_manifest(manifest, log)
        edge = (
            _pct(picks.mean_alpha - control.mean_alpha)
            if picks.measurable and control.measurable else "—"
        )
        # Short forms so the table survives an 80-column terminal; the full
        # run_id is the manifest filename.
        short_id = manifest.run_id.rsplit("_", 1)[-1]
        style = (manifest.mandate or "none").removeprefix("equity_")
        lines.append(
            f"| {short_id} | {style} | {manifest.as_of} "
            f"| {picks.settled}/{len(picks.symbols)} | {_pct(picks.mean_alpha)} "
            f"| {control.settled}/{len(control.symbols)} | {_pct(control.mean_alpha)} | {edge} |"
        )
        if picks.measurable:
            totals["picks"].append(picks.mean_alpha)
        if control.measurable:
            totals["control"].append(control.mean_alpha)

    lines += ["", "## Verdict", ""]
    if not totals["picks"]:
        horizon_note = (
            "Nothing has settled yet. A long-horizon mandate is supposed to be "
            "silent for a while -- equity_value grades at two years -- so check "
            "the interim reviews in the decision log rather than waiting here."
        )
        lines.append(horizon_note)
    elif not totals["control"]:
        lines.append(
            "Picks have settled but no controls have. Until both arms settle, "
            "the picks' return is not evidence about the screen: it is a "
            "statement about the market over that window."
        )
    else:
        p = sum(totals["picks"]) / len(totals["picks"])
        c = sum(totals["control"]) / len(totals["control"])
        lines.append(
            f"Across {len(totals['picks'])} screen(s), picks averaged "
            f"**{_pct(p)}** alpha against **{_pct(c)}** for the controls "
            f"(edge **{_pct(p - c)}**)."
        )
        n = len(totals["picks"])
        lines.append("")
        lines.append(
            f"With {n} screen(s) this is directional, not significant -- a handful "
            "of names over one window is a small sample, and the honest read is "
            "the sign of the edge and whether it persists, not its size."
            if n < 5 else
            "Read the persistence of the sign across screens rather than the "
            "magnitude of any one of them."
        )
    return "\n".join(lines)


def _pct(x) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.1f}%"


def _fmt(x) -> str:
    return "n/a" if x is None else f"{x:+.3f}"
