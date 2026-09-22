"""Business-quality metrics: returns on capital, margins, cash conversion, balance sheet.

Pure functions over a :class:`Financials`. Nothing here calls a vendor or an
LLM, so every number is reproducible from the statements alone.

Definitions worth knowing before reading the output:

* **Operating income, not Alpha Vantage's ``ebit``.** AV's ``ebit`` is pre-tax
  income plus interest, which sweeps in equity-method income and one-off gains
  (for KO in FY2025: $17.7B against $13.8B of operating income). Returns on
  capital are meant to measure the operating business, so they use operating
  income throughout.
* **ROIC** = operating income x (1 - effective tax rate) / average invested
  capital, where invested capital = total debt + equity - cash. This includes
  goodwill and acquired intangibles: it is the return on what management
  actually paid, acquisitions included.
* **Return on tangible capital (ROTC)** strips goodwill and acquired
  intangibles out of the denominator. It is the return on the capital the
  business needs to operate -- the moat measure. A wide gap between ROTC and
  ROIC means acquisitions earn far less than the core franchise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .financials import Financials

# --- screen thresholds -------------------------------------------------------
# Each maps to one of equity_value's disqualifiers. They are stated in the tool
# output alongside the measured value, so the analyst can see how close a call
# was rather than receiving a bare verdict.

ROIC_FLOOR = 0.10               # 10y median below this: returns structurally poor
ROIC_DECLINE_PP_PER_YEAR = -0.75  # 10y trend steeper than this: deteriorating
LEVERAGE_CEILING = 3.5          # net debt / EBITDA above this threatens a downturn
COVERAGE_FLOOR = 4.0            # operating income / interest expense below this
CASH_CONVERSION_FLOOR = 0.80    # 3y median operating cash flow / net income
RECEIVABLES_GAP = 0.15          # receivables outgrowing revenue by this over 3y

DEFAULT_TAX_RATE = 0.21
MAX_TAX_RATE = 0.40


def _safe_div(num: pd.Series, den: pd.Series) -> pd.Series:
    """Ratio that is NaN, not +/-inf, where the denominator is zero or negative.

    A negative denominator (negative equity after years of buybacks, negative
    invested capital) makes a return ratio meaningless rather than negative.
    """
    den = den.where(den > 0)
    return num / den


def _average(s: pd.Series) -> pd.Series:
    """Average of this period-end and the last, the standard return denominator."""
    return ((s + s.shift(1)) / 2).fillna(s)


def annual_metrics(f: Financials) -> pd.DataFrame:
    """One row per fiscal year of the quality metrics, oldest first."""
    revenue = f.col("totalRevenue")
    operating_income = f.col("operatingIncome")
    pretax = f.col("incomeBeforeTax")
    net_income = f.col("netIncome", ("netIncomeFromContinuingOperations",))

    # Effective tax rate per year, bounded; loss years and gaps fall back to the
    # company's own median so one odd year does not swing NOPAT.
    tax_rate = _safe_div(f.col("incomeTaxExpense"), pretax).clip(0, MAX_TAX_RATE)
    median_rate = tax_rate.median()
    tax_rate = tax_rate.fillna(DEFAULT_TAX_RATE if math.isnan(median_rate) else median_rate)
    nopat = operating_income * (1 - tax_rate)

    debt = f.col("shortLongTermDebtTotal").fillna(
        f.col("longTermDebt").fillna(0) + f.col("shortTermDebt", ("currentDebt",)).fillna(0)
    )
    cash = f.col("cashAndShortTermInvestments", ("cashAndCashEquivalentsAtCarryingValue",)).fillna(0)
    equity = f.col("totalShareholderEquity")
    goodwill = f.col("goodwill").fillna(0)
    intangibles = f.col("intangibleAssetsExcludingGoodwill").fillna(0)

    invested = debt + equity - cash
    tangible = invested - goodwill - intangibles

    ocf = f.col("operatingCashflow")
    capex = f.col("capitalExpenditures").abs()
    fcf = ocf - capex
    dividends = f.col("dividendPayout", ("dividendPayoutCommonStock",)).abs()
    # Buybacks arrive as a negative "proceeds" figure on AV; the dedicated
    # payments field is empty for most filers.
    buybacks = (-f.col("proceedsFromRepurchaseOfEquity")).clip(lower=0).fillna(
        f.col("paymentsForRepurchaseOfCommonStock").abs()
    )
    ebitda = operating_income + f.col(
        "depreciationAndAmortization", ("depreciationDepletionAndAmortization",)
    ).fillna(0)
    interest = f.col("interestExpense")

    return pd.DataFrame({
        "revenue": revenue,
        "gross_margin": _safe_div(f.col("grossProfit"), revenue),
        "operating_margin": _safe_div(operating_income, revenue),
        "roic": _safe_div(nopat, _average(invested)),
        "rotc": _safe_div(nopat, _average(tangible)),
        "roe": _safe_div(net_income, _average(equity)),
        "net_income": net_income,
        "ocf": ocf,
        "fcf": fcf,
        "fcf_conversion": _safe_div(fcf, net_income),
        "ocf_to_net_income": _safe_div(ocf, net_income),
        "shares": f.col("commonStockSharesOutstanding"),
        "net_debt": debt - cash,
        "net_debt_to_ebitda": _safe_div(debt - cash, ebitda),
        "interest_coverage": _safe_div(operating_income, interest),
        "dividends": dividends,
        "buybacks": buybacks,
        "capex": capex,
        "acquisitions_and_investments": (
            -f.col("cashflowFromInvestment") - capex
        ).clip(lower=0),
        "receivables": f.col("currentNetReceivables"),
        "inventory": f.col("inventory"),
        "goodwill": goodwill,
    })


def cagr(series: pd.Series, years: int) -> float:
    """Compound annual growth over the last ``years`` intervals, or NaN.

    Undefined (NaN) when either endpoint is non-positive: a growth rate from a
    loss year, or into one, is not a number anyone should act on.
    """
    s = series.dropna()
    if len(s) < years + 1:
        return float("nan")
    start, end = s.iloc[-(years + 1)], s.iloc[-1]
    if start <= 0 or end <= 0:
        return float("nan")
    return (end / start) ** (1 / years) - 1


def trend_per_year(series: pd.Series) -> float:
    """Least-squares slope per year; NaN with fewer than four points."""
    s = series.dropna()
    if len(s) < 4:
        return float("nan")
    x = np.array([ts.year + ts.dayofyear / 366 for ts in s.index])
    return float(np.polyfit(x, s.to_numpy(dtype=float), 1)[0])


@dataclass(frozen=True)
class Screen:
    """One disqualifier check: the verdict, the measured value, and the bar."""

    name: str
    status: str       # "TRIPPED", "WATCH", "CLEAR", or "NO DATA"
    evidence: str


def _status(tripped: bool | None) -> str:
    if tripped is None:
        return "NO DATA"
    return "TRIPPED" if tripped else "CLEAR"


def _pct(x: float, digits: int = 1) -> str:
    return "n/a" if x is None or math.isnan(x) else f"{x * 100:.{digits}f}%"


def _num(x: float, digits: int = 1, suffix: str = "") -> str:
    return "n/a" if x is None or math.isnan(x) else f"{x:.{digits}f}{suffix}"


def quality_screens(
    m: pd.DataFrame, window: int = 10, ttm_cash_conversion: float = float("nan"),
) -> list[Screen]:
    """The equity_value disqualifiers that the statements can settle.

    ``ttm_cash_conversion`` (trailing-twelve-month operating cash flow / net
    income) separates a persistent cash shortfall from an episodic one. A single
    large one-off outflow -- a litigation deposit, an earn-out payment -- can
    drag two fiscal years below the floor while the business converts normally;
    when the trailing figure has recovered, the screen reports WATCH and points
    at the quarters rather than declaring the accounts suspect.
    """
    recent = m.iloc[-window:]
    screens = []

    roic_median = recent["roic"].median()
    roic_slope = trend_per_year(recent["roic"])
    poor = None if math.isnan(roic_median) else roic_median < ROIC_FLOOR
    falling = None if math.isnan(roic_slope) else roic_slope * 100 < ROIC_DECLINE_PP_PER_YEAR
    tripped = None if poor is None and falling is None else bool(poor) or bool(falling)
    screens.append(Screen(
        "Returns on capital structurally poor or deteriorating",
        _status(tripped),
        f"{len(recent)}y median ROIC {_pct(roic_median)} (floor {_pct(ROIC_FLOOR, 0)}); "
        f"trend {_num(roic_slope * 100 if not math.isnan(roic_slope) else float('nan'), 2)}pp/yr "
        f"(trips below {ROIC_DECLINE_PP_PER_YEAR}pp/yr)",
    ))

    last = m.iloc[-1]
    lev, cov = last["net_debt_to_ebitda"], last["interest_coverage"]
    lev_bad = None if math.isnan(lev) else lev > LEVERAGE_CEILING
    cov_bad = None if math.isnan(cov) else cov < COVERAGE_FLOOR
    if lev_bad is None and cov_bad is None and not math.isnan(last["net_debt"]) and last["net_debt"] <= 0:
        lev_bad = False  # net cash: leverage cannot threaten solvency
    tripped = None if lev_bad is None and cov_bad is None else bool(lev_bad) or bool(cov_bad)
    screens.append(Screen(
        "Leverage threatens solvency in an ordinary downturn",
        _status(tripped),
        f"net debt/EBITDA {_num(lev)}x (ceiling {LEVERAGE_CEILING}x); interest coverage "
        f"{_num(cov)}x (floor {COVERAGE_FLOOR}x), latest fiscal year",
    ))

    conv = m["ocf_to_net_income"].iloc[-3:].median()
    rec_growth = cagr(m["receivables"], 3)
    rev_growth = cagr(m["revenue"], 3)
    gap = rec_growth - rev_growth if not (math.isnan(rec_growth) or math.isnan(rev_growth)) else float("nan")
    conv_bad = None if math.isnan(conv) else conv < CASH_CONVERSION_FLOOR
    # CAGR gap is annualised; compare the cumulative 3y gap to the threshold.
    gap_bad = None if math.isnan(gap) else gap * 3 > RECEIVABLES_GAP
    tripped = None if conv_bad is None and gap_bad is None else bool(conv_bad) or bool(gap_bad)
    status = _status(tripped)
    recovered = (
        not math.isnan(ttm_cash_conversion)
        and ttm_cash_conversion >= CASH_CONVERSION_FLOOR
    )
    if status == "TRIPPED" and conv_bad and not gap_bad and recovered:
        status = "WATCH"
    evidence = (
        f"3y median operating cash flow / net income {_num(conv, 2)}x (floor "
        f"{CASH_CONVERSION_FLOOR}x); trailing twelve months {_num(ttm_cash_conversion, 2)}x; "
        f"receivables 3y CAGR {_pct(rec_growth)} vs revenue {_pct(rev_growth)} (trips "
        f"when receivables outgrow revenue by >{_pct(RECEIVABLES_GAP, 0)} over 3y)"
    )
    if status == "WATCH":
        evidence += (
            ". The annual shortfall has not persisted into the trailing figures: "
            "check the quarterly cash-flow table for a one-off outflow before "
            "treating this as an earnings-quality problem"
        )
    screens.append(Screen("Accounting quality: earnings not backed by cash", status, evidence))
    return screens
