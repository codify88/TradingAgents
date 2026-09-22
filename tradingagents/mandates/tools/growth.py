"""Fundamental-momentum computations for the equity_momentum mandate.

Momentum without fundamental support is a trade waiting to unwind, so this
module measures the *rate of change* rather than the level: is growth
accelerating or decelerating, are margins widening, is the growth funded by
operations or by issuance, and which way are analyst estimates moving.

Estimate revisions come from Alpha Vantage's EARNINGS_ESTIMATES, which carries
the consensus as it stood 7, 30, 60 and 90 days ago plus explicit up/down
revision counts. Revision breadth is the best-documented fundamental momentum
signal after price itself, and it usually turns before the price does.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

import pandas as pd

from .financials import _fetch
from .quality import Screen, _status

# Below this, "growth" is not what the mandate is paying up for.
MIN_REVENUE_GROWTH = 0.05
# Deceleration worth naming: growth down by this many points year on year.
DECELERATION_PP = 5.0
# Forward estimates describe today's consensus and carry no as-of date. A run
# dated a few days back is the normal case (the last trading day), and the gap
# is negligible against 30- and 90-day revision windows. A run dated months or
# years back is not: that would be reading the future into a backtest.
ESTIMATE_STALENESS_TOLERANCE_DAYS = 5


def _yoy(series: pd.Series, periods: int) -> pd.Series:
    """Year-on-year growth, NaN where the base is missing or non-positive.

    A negative base makes a growth rate meaningless (earnings crossing zero
    produce percentages that look spectacular and mean nothing), so those are
    dropped rather than reported.
    """
    base = series.shift(periods)
    out = (series - base) / base.abs()
    return out.where(base > 0)


@dataclass(frozen=True)
class GrowthTrajectory:
    """Growth rates and their direction, annual and quarterly."""

    annual_revenue_growth: pd.Series
    annual_eps_growth: pd.Series
    quarterly_revenue_growth: pd.Series
    quarterly_eps_growth: pd.Series
    gross_margin: pd.Series
    operating_margin: pd.Series
    share_count: pd.Series
    fcf: pd.Series
    net_issuance: pd.Series       # equity + debt raised, per year
    # Last four quarters' revenue against the four before: the growth rate a
    # single quarter's timing (a delivery lull, a pulled-forward order) cannot move.
    ttm_revenue_growth: float = float("nan")

    @property
    def revenue_acceleration(self) -> float:
        """Change in the year-on-year quarterly growth rate, in points.

        Positive means the growth rate itself is rising -- acceleration, which
        is what a growth-momentum thesis actually requires. A high but falling
        rate is the setup that unwinds.
        """
        g = self.quarterly_revenue_growth.dropna()
        if len(g) < 5:
            return float("nan")
        return float((g.iloc[-1] - g.iloc[-5]) * 100)

    @property
    def eps_acceleration(self) -> float:
        g = self.quarterly_eps_growth.dropna()
        if len(g) < 5:
            return float("nan")
        return float((g.iloc[-1] - g.iloc[-5]) * 100)


def _ttm_growth(quarterly: pd.Series) -> float:
    """Trailing-four-quarter growth over the prior four, or NaN.

    NaN without eight consecutive quarters or with a non-positive base, for
    the same reason :func:`_yoy` drops those.
    """
    q = quarterly.dropna()
    if len(q) < 8:
        return float("nan")
    current, prior = q.iloc[-4:].sum(), q.iloc[-8:-4].sum()
    return float((current - prior) / prior) if prior > 0 else float("nan")


def growth_trajectory(f) -> GrowthTrajectory:
    revenue = f.col("totalRevenue", ("revenue",))
    q_revenue = f.col("totalRevenue", ("revenue",), quarterly=True)
    net_income = f.col("netIncome")
    q_net_income = f.col("netIncome", quarterly=True)
    shares = f.col("commonStockSharesOutstanding", ("commonStock",))
    q_shares = f.col("commonStockSharesOutstanding", ("commonStock",), quarterly=True)

    # Per-share, so buyback-driven EPS growth is not read as business growth.
    eps = (net_income / shares).where(shares > 0)
    q_eps = (q_net_income / q_shares).where(q_shares > 0)

    gross = f.col("grossProfit")
    operating = f.col("operatingIncome", ("ebit",))
    ocf = f.col("operatingCashflow")
    capex = f.col("capitalExpenditures")

    return GrowthTrajectory(
        annual_revenue_growth=_yoy(revenue, 1),
        annual_eps_growth=_yoy(eps, 1),
        quarterly_revenue_growth=_yoy(q_revenue, 4),
        quarterly_eps_growth=_yoy(q_eps, 4),
        gross_margin=(gross / revenue).where(revenue > 0),
        operating_margin=(operating / revenue).where(revenue > 0),
        share_count=shares,
        fcf=ocf - capex.abs(),
        net_issuance=(
            f.col("proceedsFromIssuanceOfCommonStock").fillna(0)
            + f.col("proceedsFromIssuanceOfLongTermDebtAndCapitalSecuritiesNet").fillna(0)
        ),
        ttm_revenue_growth=_ttm_growth(q_revenue),
    )


@dataclass(frozen=True)
class Revisions:
    """Consensus drift and revision breadth for one forecast horizon."""

    horizon: str
    period: str
    analysts: float
    eps_now: float
    eps_30d_ago: float
    eps_90d_ago: float
    revenue_now: float
    up_30d: float
    down_30d: float

    @property
    def drift_30d(self) -> float:
        """Percent change in the consensus EPS over 30 days."""
        if not self.eps_30d_ago or math.isnan(self.eps_30d_ago) or self.eps_30d_ago <= 0:
            return float("nan")
        return (self.eps_now - self.eps_30d_ago) / self.eps_30d_ago

    @property
    def drift_90d(self) -> float:
        if not self.eps_90d_ago or math.isnan(self.eps_90d_ago) or self.eps_90d_ago <= 0:
            return float("nan")
        return (self.eps_now - self.eps_90d_ago) / self.eps_90d_ago

    @property
    def breadth_30d(self) -> float:
        """(up - down) / total revisions over 30 days, in [-1, 1]."""
        total = (self.up_30d or 0) + (self.down_30d or 0)
        if not total or math.isnan(total):
            return float("nan")
        return ((self.up_30d or 0) - (self.down_30d or 0)) / total


def _f(row: dict, key: str) -> float:
    try:
        return float(row.get(key))
    except (TypeError, ValueError):
        return float("nan")


def estimate_revisions(ticker: str, as_of: pd.Timestamp) -> list[Revisions]:
    """Forward estimates and their revision history, nearest horizon first.

    Estimates carry no as-of date, so a genuinely historical run cannot be served
    them honestly: they describe today's consensus, not the consensus on a past
    date. A backtest therefore gets an empty list rather than tomorrow's
    information, while a run dated within
    :data:`ESTIMATE_STALENESS_TOLERANCE_DAYS` is served and the caller states the
    caveat in the report.
    """
    age = (pd.Timestamp.today().normalize() - as_of.normalize()).days
    if age > ESTIMATE_STALENESS_TOLERANCE_DAYS:
        return []
    payload = json.loads(_fetch("EARNINGS_ESTIMATES", ticker.strip().upper()))
    rows = payload.get("estimates") if isinstance(payload, dict) else None
    if not rows:
        return []

    out = []
    for row in rows:
        date = str(row.get("date", ""))
        if not date or pd.Timestamp(date) <= as_of:
            continue  # a period already ended is not a forecast
        out.append(Revisions(
            horizon=str(row.get("horizon", "")),
            period=date,
            analysts=_f(row, "eps_estimate_analyst_count"),
            eps_now=_f(row, "eps_estimate_average"),
            eps_30d_ago=_f(row, "eps_estimate_average_30_days_ago"),
            eps_90d_ago=_f(row, "eps_estimate_average_90_days_ago"),
            revenue_now=_f(row, "revenue_estimate_average"),
            up_30d=_f(row, "eps_estimate_revision_up_trailing_30_days"),
            down_30d=_f(row, "eps_estimate_revision_down_trailing_30_days"),
        ))
    return sorted(out, key=lambda r: r.period)[:4]


def growth_screens(
    g: GrowthTrajectory, revisions: list[Revisions], multiple_expanding: bool | None,
    price_12m: float,
) -> list[Screen]:
    """The equity_momentum disqualifiers the fundamentals can settle."""
    screens: list[Screen] = []

    q = g.quarterly_revenue_growth.dropna()
    latest = float(q.iloc[-1]) if len(q) else float("nan")
    accel = g.revenue_acceleration
    decelerating = None if math.isnan(accel) else accel < -DECELERATION_PP
    if decelerating is None or multiple_expanding is None:
        status = "NO DATA"
    elif decelerating and multiple_expanding:
        status = "TRIPPED"
    elif decelerating:
        # Deceleration alone is not the disqualifier -- the mandate's screen is
        # deceleration *while the multiple expands* -- but it is the half that
        # usually arrives first, so it is surfaced rather than cleared away.
        status = "WATCH"
    else:
        status = "CLEAR"
    screens.append(Screen(
        "Growth decelerating while the multiple is still expanding",
        status,
        f"latest quarterly revenue growth {_pct(latest)}, "
        f"{'n/a' if math.isnan(accel) else f'{accel:+.1f}pp'} vs four quarters earlier; "
        f"multiple {'expanding' if multiple_expanding else 'not expanding' if multiple_expanding is not None else 'n/a'} "
        f"(12-month price return against trailing per-share earnings growth)",
    ))

    breadth = revisions[0].breadth_30d if revisions else float("nan")
    drift = revisions[0].drift_90d if revisions else float("nan")
    diverging = None
    if not math.isnan(price_12m) and not math.isnan(breadth):
        # Price up while estimates are cut: the classic pre-unwind signature.
        diverging = price_12m > 0 and breadth < -0.2
    screens.append(Screen(
        "Price momentum and fundamental momentum point in opposite directions",
        _status(diverging),
        f"12m price {_pct(price_12m)}, 30-day revision breadth "
        f"{'n/a' if math.isnan(breadth) else f'{breadth:+.2f}'}, "
        f"90-day consensus drift {_pct(drift)}",
    ))

    # Judged on the trailing year, not one quarter: a single quarter below the
    # floor (TSLA's -11.8% at 2025-09-02) can be timing, and tripping on it
    # barred a name whose disqualifier the year had not yet shown. One weak
    # reading of the two is a WATCH, so the analyst says which one to believe.
    ttm = g.ttm_revenue_growth
    readings = [x < MIN_REVENUE_GROWTH for x in (ttm, latest) if not math.isnan(x)]
    if not readings:
        status = "NO DATA"
    elif math.isnan(ttm):
        status = "WATCH" if readings[0] else "CLEAR"  # one quarter alone never trips
    elif all(readings):
        status = "TRIPPED"
    elif any(readings):
        status = "WATCH"
    else:
        status = "CLEAR"
    screens.append(Screen(
        "Not actually a growth business",
        status,
        f"trailing-four-quarter revenue growth {_pct(ttm)}, latest quarter {_pct(latest)}, "
        f"against a {MIN_REVENUE_GROWTH * 100:.0f}% floor",
    ))
    return screens


def _pct(x) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.1f}%"
