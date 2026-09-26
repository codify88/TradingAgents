"""Point-in-time value facts for the lab: what the value screen knew, date by date.

For each schedule date and each of the most liquid eligible names, the same
code the live value screen runs: statements as public on that date
(``load_financials`` keeps a period out until its earnings release, else the
SEC filing deadline; delisted companies come from EDGAR), the quality screens
that tripped, and the cheapness measures an ordering can rank on.

Two limits the lab cannot remove, stated in its report: the vendor serves
restated figures, not the numbers as first published; and an annual period is
dated by its earnings release, when the cash-flow statement may only follow
with the 10-K. Statements start in 2006, so a 2012 screen compares a company
with about six years of its own history rather than ten.

Computed once per (symbol, date) and kept in ``lab/value_facts.pkl``; every
variant reads the same facts, so a variant costs no requests.
"""

from __future__ import annotations

import logging
import math
import pickle
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from .panel import lab_dir

logger = logging.getLogger(__name__)

# Measures an ordering can rank on; higher is cheaper in every one.
MEASURES = {
    "fcf_pct": "FCF yield percentile in the company's own history (the live value ordering)",
    "fcf_yield": "trailing FCF yield, across companies",
    "ey_pct": "earnings yield percentile in the company's own history",
    "ebit_pct": "EBIT / EV percentile in the company's own history",
}


@dataclass
class Facts:
    tripped: list[str] = field(default_factory=list)
    values: dict[str, float] = field(default_factory=dict)
    years: int = 0                 # fiscal years of history the percentiles use
    error: str = ""                # why the name could not be judged (excluded, as live)
    vendor: bool = False           # the error was the vendor, not the company
    metrics: dict[str, float] = field(default_factory=dict)  # what the quality rules read (quality_rules.py)


def _pct(current: float, history) -> float:
    from tradingagents.mandates.tools.valuation import percentile_in_history

    return percentile_in_history(current, history)


def compute(symbol: str, as_of: str) -> Facts:
    """The live value screen's view of ``symbol`` on ``as_of``."""
    from tradingagents.mandates.tools import valuation as val
    from tradingagents.mandates.tools.financials import FinancialsUnavailable, load_financials
    from tradingagents.mandates.tools.quality import annual_metrics, quality_screens
    from tradingagents.screener.screen import _long_prices

    try:
        fin = load_financials(symbol, as_of)
    except FinancialsUnavailable as exc:
        return Facts(error=f"fundamentals unavailable ({exc})"[:200],
                     vendor="Invalid API call" in str(exc) or "non-JSON" in str(exc))
    except Exception as exc:
        return Facts(error=f"fundamentals unavailable ({type(exc).__name__})", vendor=True)
    try:
        metrics = annual_metrics(fin)
        tripped = [s.name for s in quality_screens(metrics) if s.status == "TRIPPED"]
        from .quality_rules import metrics_of

        rule_inputs = metrics_of(metrics)
        closes = _long_prices(fin)
        if closes.empty:
            return Facts(tripped=tripped, error="no price history", vendor=True)
        current = val.snapshot(fin, closes)
        history = val.historical_multiples(fin, closes)
    except Exception as exc:
        return Facts(error=f"screens could not be computed ({type(exc).__name__})")
    values = dict.fromkeys(MEASURES, math.nan)
    if current is not None and not history.empty and current.market_cap > 0:
        mcap = current.market_cap
        values["fcf_yield"] = current.ttm_fcf / mcap
        if "fcf_yield" in history:
            values["fcf_pct"] = _pct(current.ttm_fcf / mcap, history["fcf_yield"])
        if "pe" in history:
            values["ey_pct"] = _pct(current.ttm_net_income / mcap, 1 / history["pe"])
        if "ev_to_ebit" in history and current.enterprise_value > 0:
            values["ebit_pct"] = _pct(current.ttm_operating_income / current.enterprise_value,
                                      1 / history["ev_to_ebit"])
    return Facts(tripped=tripped, values=values, years=len(history), metrics=rule_inputs)


# --- the cache ----------------------------------------------------------------------


def _path(config: dict):
    return lab_dir(config) / "value_facts.pkl"


def load_facts(config: dict) -> dict[tuple[str, str], Facts]:
    try:
        with _path(config).open("rb") as fh:
            return pickle.load(fh)
    except (OSError, pickle.UnpicklingError, EOFError):
        return {}


def save_facts(config: dict, facts: dict[tuple[str, str], Facts]) -> None:
    tmp = _path(config).with_suffix(".tmp")
    with tmp.open("wb") as fh:
        pickle.dump(facts, fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(_path(config))


def needed(ctx, dates: Iterable[int], pool: int, min_dollar_volume: float,
           horizon: int = 0) -> list[tuple[str, str]]:
    """(symbol, date) pairs a value replay reads: the ``pool`` most liquid eligible names on
    each date whose ``horizon``-day holding has finished."""
    out = []
    for i in dates:
        if i + 1 + horizon >= len(ctx.dates):
            continue
        day = ctx.dates[i].strftime("%Y-%m-%d")
        for j in ctx.liquid_eligible(i, min_dollar_volume, pool):
            out.append((str(ctx.symbols[j]), day))
    return out


@dataclass
class FactsReport:
    needed: int = 0
    computed: int = 0
    vendor_errors: int = 0
    stopped: str = "done"


def fill(config: dict, pairs: list[tuple[str, str]], compute_one: Callable[[str, str], Facts] = compute,
         deadline: datetime | None = None, retry_vendor: bool = True,
         progress: Callable[[int, int], None] | None = None, refresh: bool = False) -> FactsReport:
    """Compute what the cache lacks (and, by default, retry earlier vendor errors).
    Runs inside ``prefer_stored``: statements and prices already stored are not
    asked for again, whatever their age."""
    from tradingagents.datastore import prefer_stored

    facts = load_facts(config)
    # By symbol, so one company's statements and prices are parsed while hot.
    # ``refresh``: recompute judged facts cached before they carried their
    # metrics, so the quality rule sets can be applied to every date.
    todo = sorted(p for p in dict.fromkeys(pairs)
                  if p not in facts or (retry_vendor and facts[p].vendor)
                  or (refresh and not facts[p].error and not getattr(facts[p], "metrics", None)))
    report = FactsReport(needed=len(todo))
    with prefer_stored():
        for n, (symbol, day) in enumerate(todo, 1):
            if deadline and datetime.now(UTC) >= deadline:
                report.stopped = "deadline"
                break
            f = compute_one(symbol, day)
            facts[(symbol, day)] = f
            report.computed += 1
            report.vendor_errors += f.vendor
            if n % 200 == 0:
                save_facts(config, facts)
                if progress:
                    progress(n, len(todo))
    save_facts(config, facts)
    return report


def coverage(facts: dict[tuple[str, str], Facts], pairs: list[tuple[str, str]]) -> dict[str, float]:
    pairs = list(dict.fromkeys(pairs))
    got = [facts.get(p) for p in pairs]
    known = [f for f in got if f is not None]
    judged = [f for f in known if not f.error]
    years = [f.years for f in judged if f.years]
    return {"pairs": len(pairs), "computed": len(known), "judged": len(judged),
            "vendor_errors": sum(f.vendor for f in known),
            "median_years": float(np.median(years)) if years else math.nan}
