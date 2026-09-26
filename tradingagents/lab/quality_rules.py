"""The value screen's quality exclusions as rule sets the lab can compare.

The live rules (``mandates/tools/quality.py``) exclude 73% of the most liquid
names, Apple in 65% of months and Microsoft in 70%: "returns on capital
deteriorating" trips on any downward trend steeper than 0.75pp a year however
high the level, and "earnings not backed by cash" trips on receivables growth
alone. The rule sets here keep the live thresholds and change only those two
tests, so a difference in the lab is the rule, not a threshold moved to fit.

Named 2026-09-26, before any of them was run:

- ``rel_roic``: a falling return on capital trips only while the latest year's
  return is also below ``ROIC_HEALTHY`` -- heading toward the floor, not down
  from a high level;
- ``rel_acct``: accounting trips on weak cash conversion; receivables
  outgrowing revenue counts only when conversion is also below 1.0;
- ``rel``: both.

Each works from the same ``metrics`` the live screen computes (``metrics_of``),
and ``live`` reproduces the live rules from those metrics exactly, which the
tests check against ``quality_screens``.
"""

from __future__ import annotations

import math

from tradingagents.mandates.tools import quality as q

ROIC_HEALTHY = 0.15      # a return this high is not "deteriorating" toward anything
CONVERSION_OK = 1.0      # with conversion at least this, receivables growth is not a flag

POOR = "Returns on capital structurally poor or deteriorating"
LEVERAGE = "Leverage threatens solvency in an ordinary downturn"
ACCOUNTS = "Accounting quality: earnings not backed by cash"


def metrics_of(m, window: int = 10) -> dict[str, float]:
    """The numbers the quality rules read, from ``quality.annual_metrics``."""
    recent = m.iloc[-window:]
    roic = recent["roic"]
    last = m.iloc[-1]
    rec, rev = q.cagr(m["receivables"], 3), q.cagr(m["revenue"], 3)
    return {
        "roic_median": float(roic.median()),
        "roic_slope": float(q.trend_per_year(roic)),
        "roic_latest": float(roic.dropna().iloc[-1]) if roic.notna().any() else math.nan,
        "leverage": float(last["net_debt_to_ebitda"]),
        "coverage": float(last["interest_coverage"]),
        "net_debt": float(last["net_debt"]),
        "conversion": float(m["ocf_to_net_income"].iloc[-3:].median()),
        "receivables_gap": (rec - rev) if not (math.isnan(rec) or math.isnan(rev)) else math.nan,
    }


def _nan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _leverage(x: dict) -> bool:
    lev, cov = x["leverage"], x["coverage"]
    lev_bad = None if _nan(lev) else lev > q.LEVERAGE_CEILING
    cov_bad = None if _nan(cov) else cov < q.COVERAGE_FLOOR
    if lev_bad is None and cov_bad is None and not _nan(x["net_debt"]) and x["net_debt"] <= 0:
        lev_bad = False  # net cash
    return bool(lev_bad) or bool(cov_bad)


def _poor(x: dict, relative: bool) -> bool:
    poor = not _nan(x["roic_median"]) and x["roic_median"] < q.ROIC_FLOOR
    falling = not _nan(x["roic_slope"]) and x["roic_slope"] * 100 < q.ROIC_DECLINE_PP_PER_YEAR
    if relative and falling:
        falling = _nan(x["roic_latest"]) or x["roic_latest"] < ROIC_HEALTHY
    return poor or falling


def _accounts(x: dict, relative: bool) -> bool:
    conv_bad = not _nan(x["conversion"]) and x["conversion"] < q.CASH_CONVERSION_FLOOR
    gap_bad = not _nan(x["receivables_gap"]) and x["receivables_gap"] * 3 > q.RECEIVABLES_GAP
    if relative and gap_bad:
        gap_bad = _nan(x["conversion"]) or x["conversion"] < CONVERSION_OK
    return conv_bad or gap_bad


def _rules(relative_roic: bool, relative_accounts: bool):
    def tripped(x: dict) -> list[str]:
        out = []
        if _poor(x, relative_roic):
            out.append(POOR)
        if _leverage(x):
            out.append(LEVERAGE)
        if _accounts(x, relative_accounts):
            out.append(ACCOUNTS)
        return out
    return tripped


RULES = {
    "live": _rules(False, False),
    "rel_roic": _rules(True, False),
    "rel_acct": _rules(False, True),
    "rel": _rules(True, True),
}
