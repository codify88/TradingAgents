"""Valuation: what the price implies, against the company's own history.

Two questions a value analyst has to answer with numbers rather than adjectives:

1. **Is the stock cheap or dear relative to itself?** Current EV/EBIT, P/E and
   free-cash-flow yield, set against the same multiples at each of the last ten
   fiscal year ends.
2. **What growth is the price already paying for?** A reverse DCF solves for the
   free-cash-flow growth rate that justifies today's market capitalisation, so
   the analyst can compare it with what the business has actually delivered.
   This is the "no margin of safety" disqualifier made measurable.

Pure functions; prices and statements come in, numbers go out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from .financials import Financials, close_on_or_before
from .quality import Screen, _pct, annual_metrics, cagr

DEFAULT_DISCOUNT_RATE = 0.08
DEFAULT_TERMINAL_GROWTH = 0.025
DEFAULT_YEARS = 10
# Every reverse DCF is shown across this grid: the implied growth moves by
# several points per point of discount rate, so a single rate would present an
# assumption as a finding.
DISCOUNT_RATE_GRID = (0.07, 0.08, 0.09)
# The screen is judged at the generous end of the grid. A low-beta staple can
# fairly argue for 7%; if the price still demands more growth than the record
# at 7%, the verdict does not depend on a contestable cost of capital.
SCREEN_DISCOUNT_RATE = 0.07
# Implied growth this far above the business's own record trips the margin-of-
# safety screen. Two points absorbs the noise in any growth estimate.
GROWTH_TOLERANCE = 0.02
NORMALIZATION_YEARS = 5

_GROWTH_LOW, _GROWTH_HIGH = -0.50, 1.00


@dataclass(frozen=True)
class Snapshot:
    """Valuation as of one date, from the last close and the last known filings."""

    price_date: pd.Timestamp
    price: float
    shares: float
    market_cap: float
    debt: float
    cash: float
    enterprise_value: float
    ttm_revenue: float
    ttm_operating_income: float
    ttm_net_income: float
    ttm_fcf: float
    ttm_dividends: float
    normalized_fcf: float

    @property
    def ev_to_ebit(self) -> float:
        return _ratio(self.enterprise_value, self.ttm_operating_income)

    @property
    def pe(self) -> float:
        return _ratio(self.market_cap, self.ttm_net_income)

    @property
    def fcf_yield(self) -> float:
        return _ratio(self.ttm_fcf, self.market_cap, allow_negative_numerator=True)

    @property
    def normalized_fcf_yield(self) -> float:
        return _ratio(self.normalized_fcf, self.market_cap, allow_negative_numerator=True)

    @property
    def dividend_yield(self) -> float:
        return _ratio(self.ttm_dividends, self.market_cap)


def _ratio(num: float, den: float, allow_negative_numerator: bool = False) -> float:
    """num/den, NaN where the result would mislead (den <= 0, or a negative multiple)."""
    if any(math.isnan(x) for x in (num, den)) or den <= 0:
        return float("nan")
    if num <= 0 and not allow_negative_numerator:
        return float("nan")
    return num / den


def _debt_and_cash(f: Financials) -> tuple[float, float]:
    debt = f.latest("shortLongTermDebtTotal")
    if math.isnan(debt):
        debt = (
            _zero_if_nan(f.latest("longTermDebt"))
            + _zero_if_nan(f.latest("shortTermDebt", ("currentDebt",)))
        )
    cash = f.latest("cashAndShortTermInvestments", ("cashAndCashEquivalentsAtCarryingValue",))
    return debt, _zero_if_nan(cash)


def _zero_if_nan(x: float) -> float:
    return 0.0 if math.isnan(x) else x


def normalized_fcf(f: Financials, years: int = NORMALIZATION_YEARS) -> float:
    """Median annual free cash flow over the last ``years`` fiscal years.

    Owner earnings through the cycle: one depressed or inflated year -- a
    litigation deposit, an earn-out, a working-capital unwind -- moves a median
    far less than it moves a single year or a trailing sum.
    """
    fcf = annual_metrics(f)["fcf"].dropna().iloc[-years:]
    return float(fcf.median()) if len(fcf) else float("nan")


def snapshot(f: Financials, prices: pd.Series) -> Snapshot | None:
    """Current valuation, or None when there is no price at or just before as_of."""
    close = close_on_or_before(prices, f.as_of)
    if close is None:
        return None
    price_date, price = close
    shares = f.latest("commonStockSharesOutstanding")
    market_cap = price * shares
    debt, cash = _debt_and_cash(f)
    capex = f.ttm("capitalExpenditures")
    ocf = f.ttm("operatingCashflow")
    return Snapshot(
        price_date=price_date,
        price=price,
        shares=shares,
        market_cap=market_cap,
        debt=debt,
        cash=cash,
        enterprise_value=market_cap + debt - cash,
        ttm_revenue=f.ttm("totalRevenue"),
        ttm_operating_income=f.ttm("operatingIncome"),
        ttm_net_income=f.ttm("netIncome"),
        ttm_fcf=ocf - abs(capex),
        ttm_dividends=abs(f.ttm("dividendPayout", ("dividendPayoutCommonStock",))),
        normalized_fcf=normalized_fcf(f),
    )


def historical_multiples(f: Financials, prices: pd.Series) -> pd.DataFrame:
    """EV/EBIT, P/E, FCF and dividend yield at each fiscal year end with a price."""
    m = annual_metrics(f)
    operating_income = f.col("operatingIncome")
    rows = {}
    for end in m.index:
        close = close_on_or_before(prices, end)
        if close is None:
            continue
        _, price = close
        row = m.loc[end]
        market_cap = price * row["shares"]
        ev = market_cap + row["net_debt"]
        rows[end] = {
            "price": price,
            "market_cap": market_cap,
            "ev_to_ebit": _ratio(ev, operating_income.loc[end]),
            "pe": _ratio(market_cap, row["net_income"]),
            "fcf_yield": _ratio(row["fcf"], market_cap, allow_negative_numerator=True),
            "dividend_yield": _ratio(row["dividends"], market_cap),
        }
    return pd.DataFrame.from_dict(rows, orient="index")


def percentile_in_history(current: float, history: pd.Series) -> float:
    """Share of historical observations strictly below ``current`` (0..1)."""
    h = history.dropna()
    if math.isnan(current) or h.empty:
        return float("nan")
    return float((h < current).mean())


# --- discounted cash flow -----------------------------------------------------


def dcf_value(
    base_fcf: float,
    growth: float,
    discount_rate: float = DEFAULT_DISCOUNT_RATE,
    terminal_growth: float = DEFAULT_TERMINAL_GROWTH,
    years: int = DEFAULT_YEARS,
) -> float:
    """Present value of ``base_fcf`` growing at ``growth`` for ``years``, then a
    Gordon terminal value at ``terminal_growth``."""
    if discount_rate <= terminal_growth:
        raise ValueError("discount rate must exceed terminal growth")
    pv, fcf = 0.0, base_fcf
    for t in range(1, years + 1):
        fcf *= 1 + growth
        pv += fcf / (1 + discount_rate) ** t
    terminal = fcf * (1 + terminal_growth) / (discount_rate - terminal_growth)
    return pv + terminal / (1 + discount_rate) ** years


def reverse_dcf(
    market_cap: float,
    base_fcf: float,
    discount_rate: float = DEFAULT_DISCOUNT_RATE,
    terminal_growth: float = DEFAULT_TERMINAL_GROWTH,
    years: int = DEFAULT_YEARS,
) -> float:
    """The annual FCF growth over ``years`` that makes the DCF equal ``market_cap``.

    NaN when the base cash flow is not positive (no growth rate can turn a
    loss into today's value). Clamped to the search range at its ends, so a
    price that needs more than 100% a year reads as 100%, not as a crash.
    """
    if any(math.isnan(x) for x in (market_cap, base_fcf)) or base_fcf <= 0 or market_cap <= 0:
        return float("nan")
    lo, hi = _GROWTH_LOW, _GROWTH_HIGH
    if dcf_value(base_fcf, lo, discount_rate, terminal_growth, years) >= market_cap:
        return lo
    if dcf_value(base_fcf, hi, discount_rate, terminal_growth, years) <= market_cap:
        return hi
    for _ in range(200):
        mid = (lo + hi) / 2
        if dcf_value(base_fcf, mid, discount_rate, terminal_growth, years) < market_cap:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return (lo + hi) / 2


def growth_record(f: Financials, years: int = 10) -> tuple[float, str]:
    """The business's own growth record, and which series it came from.

    The highest of revenue, operating-income and free-cash-flow CAGR: a
    deliberately generous reading, so that when the price still demands more
    than this, the margin-of-safety screen is tripping on something real.
    Operating income matters most for companies that shrink revenue on purpose
    (KO refranchised its bottlers: revenue grew 0.8% a year over the decade to
    FY2025 while operating income grew 4.7%).
    """
    m = annual_metrics(f)
    span = min(years, len(m.dropna(subset=["revenue"])) - 1)
    if span < 3:
        return float("nan"), "insufficient history"
    candidates = [
        (cagr(m["revenue"], span), f"{span}y revenue CAGR"),
        (cagr(f.col("operatingIncome"), span), f"{span}y operating-income CAGR"),
        (cagr(m["fcf"], span), f"{span}y free-cash-flow CAGR"),
    ]
    candidates = [c for c in candidates if not math.isnan(c[0])]
    if not candidates:
        return float("nan"), "no positive-endpoint growth series"
    return max(candidates)


def margin_of_safety_screen(
    implied: dict[str, float], record: float, record_label: str,
) -> Screen:
    """equity_value's "no margin of safety" disqualifier, measured.

    ``implied`` maps a FCF base ("normalized", "trailing") to the growth the
    price implies on it at ``SCREEN_DISCOUNT_RATE``. TRIPPED when every base
    demands more than the record plus tolerance; WATCH when only some do,
    because then the verdict turns on which cash-flow base is representative --
    a judgement the analyst should make explicitly, not one the screen hides.
    """
    usable = {k: v for k, v in implied.items() if not math.isnan(v)}
    detail = (
        f"at a {_pct(SCREEN_DISCOUNT_RATE, 0)} discount rate: "
        + "; ".join(f"{k} FCF base implies {_pct(v)}/yr" for k, v in implied.items())
    )
    bar = f"record: {record_label} {_pct(record)} (+{_pct(GROWTH_TOLERANCE, 0)} tolerance)"
    if not usable or math.isnan(record):
        return Screen("No margin of safety: price embeds an optimistic case", "NO DATA",
                      f"{detail}; {bar}")
    over = [k for k, v in usable.items() if v > record + GROWTH_TOLERANCE]
    if len(over) == len(usable):
        status = "TRIPPED"
    elif over:
        status = "WATCH"
    else:
        status = "CLEAR"
    return Screen("No margin of safety: price embeds an optimistic case", status,
                  f"{detail}; {bar}")
