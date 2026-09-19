"""P2 deterministic value tools: statements loader, quality, valuation, LLM tools.

Payloads are synthetic but shaped exactly like Alpha Vantage's (numbers as
strings, gaps as the string "None"), so the loader is exercised end to end and
every expected figure below can be worked out by hand.
"""

import json
import math
from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.mandates.tools import financials as fin, valuation as val, value_tools as vt
from tradingagents.mandates.tools.quality import (
    annual_metrics,
    cagr,
    quality_screens,
    trend_per_year,
)

INCOME = {"totalRevenue", "grossProfit", "operatingIncome", "ebit", "netIncome",
          "incomeTaxExpense", "incomeBeforeTax", "interestExpense", "depreciationAndAmortization"}
BALANCE = {"totalShareholderEquity", "shortLongTermDebtTotal", "longTermDebt", "shortTermDebt",
           "cashAndShortTermInvestments", "commonStockSharesOutstanding", "goodwill",
           "intangibleAssetsExcludingGoodwill", "currentNetReceivables", "inventory"}
CASH_FLOW = {"operatingCashflow", "capitalExpenditures", "dividendPayout",
             "proceedsFromRepurchaseOfEquity", "cashflowFromInvestment", "cfNetIncome"}

# A steady business whose ratios are easy to verify by hand:
#   NOPAT = 250 x (1 - 50/250) = 200; invested capital = 400 + 800 - 200 = 1000
#   ROIC = 20%; tangible capital = 1000 - 300 - 100 = 600 -> ROTC = 33.3%
#   ROE = 190 / 800 = 23.75%; FCF = 240 - 40 = 200; FCF/NI = 1.05
STEADY = {
    "totalRevenue": 1000, "grossProfit": 600, "operatingIncome": 250, "ebit": 999,
    "netIncome": 190, "incomeTaxExpense": 50, "incomeBeforeTax": 250, "interestExpense": 20,
    "depreciationAndAmortization": 50,
    "totalShareholderEquity": 800, "shortLongTermDebtTotal": 400, "cashAndShortTermInvestments": 200,
    "commonStockSharesOutstanding": 100, "goodwill": 300, "intangibleAssetsExcludingGoodwill": 100,
    "currentNetReceivables": 100, "inventory": 50,
    "operatingCashflow": 240, "capitalExpenditures": 40, "dividendPayout": 100,
    "proceedsFromRepurchaseOfEquity": -30, "cashflowFromInvestment": -60, "cfNetIncome": 111,
}


def _report(end: str, fields: dict, names: set) -> dict:
    out = {"fiscalDateEnding": end, "reportedCurrency": "USD"}
    for k, v in fields.items():
        if k in names:
            key = "netIncome" if k == "cfNetIncome" else k
            out[key] = "None" if v is None else str(v)
    return out


def company(annual: dict, quarterly: dict | None = None, reported: dict | None = None) -> dict:
    """AV-shaped payloads keyed by function, newest report first as AV sends them."""
    quarterly = quarterly or {}
    payloads = {}
    for fn, names in (("INCOME_STATEMENT", INCOME), ("BALANCE_SHEET", BALANCE), ("CASH_FLOW", CASH_FLOW)):
        payloads[fn] = {
            "symbol": "TEST",
            "annualReports": [_report(e, f, names) for e, f in sorted(annual.items(), reverse=True)],
            "quarterlyReports": [_report(e, f, names) for e, f in sorted(quarterly.items(), reverse=True)],
        }
    payloads["EARNINGS"] = {"quarterlyEarnings": [
        {"fiscalDateEnding": e, "reportedDate": d} for e, d in (reported or {}).items()
    ]}
    return payloads


def steady_years(first=2014, last=2025, **overrides):
    return {f"{y}-12-31": {**STEADY, **overrides} for y in range(first, last + 1)}


def quarter_rows(year=2025, **overrides):
    q = {k: (v / 4 if k in INCOME | CASH_FLOW else v) for k, v in STEADY.items()}
    return {f"{year}-{m}": {**q, **overrides} for m in ("03-31", "06-30", "09-30", "12-31")}


@pytest.fixture
def av():
    """Serve a synthetic company through the loader's single fetch point."""
    state = {"payloads": company(steady_years())}

    def fake_fetch(function, symbol):
        return json.dumps(state["payloads"][function])

    with patch.object(fin, "_fetch", side_effect=fake_fetch):
        yield state


# --- loader --------------------------------------------------------------------


class TestLoader:

    def test_parses_strings_and_none(self, av):
        av["payloads"] = company(steady_years(inventory=None))
        f = fin.load_financials("TEST", "2026-09-17")
        assert f.annual["totalRevenue"].iloc[-1] == 1000.0
        assert math.isnan(f.annual["inventory"].iloc[-1])

    def test_income_statement_net_income_wins_over_cash_flow(self, av):
        f = fin.load_financials("TEST", "2026-09-17")
        assert f.annual["netIncome"].iloc[-1] == 190.0  # not the cash-flow 111

    def test_period_is_withheld_until_it_was_reported(self, av):
        av["payloads"] = company(steady_years(), reported={"2025-12-31": "2026-02-10"})
        before = fin.load_financials("TEST", "2026-02-09")
        on = fin.load_financials("TEST", "2026-02-10")
        assert before.annual.index[-1] == pd.Timestamp("2024-12-31")
        assert on.annual.index[-1] == pd.Timestamp("2025-12-31")

    def test_filing_deadline_is_the_fallback_when_no_report_date(self, av):
        # FY2025 has no reportedDate: admitted 90 days after year end (31 Mar), not before.
        assert fin.load_financials("TEST", "2026-03-31").annual.index[-1].year == 2025
        assert fin.load_financials("TEST", "2026-03-30").annual.index[-1].year == 2024

    def test_upstream_fiscal_date_filter_would_have_leaked(self, av):
        """FY2025 ends 31 Dec but is not public until February (#1251 class of leak)."""
        av["payloads"] = company(steady_years(), reported={"2025-12-31": "2026-02-10"})
        f = fin.load_financials("TEST", "2026-01-15")
        assert pd.Timestamp("2025-12-31") not in f.annual.index

    def test_nothing_public_raises(self, av):
        with pytest.raises(fin.FinancialsUnavailable, match="no annual statements"):
            fin.load_financials("TEST", "2010-01-01")

    def test_vendor_error_message_raises(self, av):
        av["payloads"]["INCOME_STATEMENT"] = {"Error Message": "Invalid API call."}
        with pytest.raises(fin.FinancialsUnavailable, match="Invalid API call"):
            fin.load_financials("TEST", "2026-09-17")

    def test_ttm_sums_four_quarters(self, av):
        av["payloads"] = company(steady_years(), quarter_rows())
        f = fin.load_financials("TEST", "2026-09-17")
        assert f.ttm("operatingCashflow") == pytest.approx(240.0)

    def test_ttm_is_nan_with_fewer_than_four_quarters(self, av):
        q = quarter_rows()
        q.pop("2025-12-31")
        av["payloads"] = company(steady_years(), q)
        assert math.isnan(fin.load_financials("TEST", "2026-09-17").ttm("operatingCashflow"))

    def test_ttm_is_nan_across_a_filing_gap(self, av):
        q = {**quarter_rows(2024), **quarter_rows(2025)}
        for end in ("2025-03-31", "2025-06-30", "2025-09-30"):
            q.pop(end)  # last four known quarters now span 15 months
        av["payloads"] = company(steady_years(), q)
        assert math.isnan(fin.load_financials("TEST", "2026-09-17").ttm("operatingCashflow"))

    def test_latest_prefers_the_quarterly_balance_sheet(self, av):
        av["payloads"] = company(steady_years(), quarter_rows(commonStockSharesOutstanding=90))
        assert fin.load_financials("TEST", "2026-09-17").latest("commonStockSharesOutstanding") == 90


class TestPrices:

    def test_history_never_extends_past_as_of(self):
        idx = pd.date_range("2026-09-10", "2026-09-20", freq="D")
        full = pd.Series(range(len(idx)), index=idx, dtype=float)
        with patch.object(fin, "_price_history", return_value=full):
            got = fin.price_history("TEST", pd.Timestamp("2026-09-01"), pd.Timestamp("2026-09-15"))
        assert got.index.max() == pd.Timestamp("2026-09-15")

    def test_close_on_or_before_looks_back_at_most_a_week(self):
        prices = pd.Series([10.0], index=[pd.Timestamp("2026-01-02")])
        assert fin.close_on_or_before(prices, pd.Timestamp("2026-01-05")) == (pd.Timestamp("2026-01-02"), 10.0)
        assert fin.close_on_or_before(prices, pd.Timestamp("2026-01-20")) is None


# --- quality --------------------------------------------------------------------


class TestQualityMetrics:

    def metrics(self, av, **overrides):
        av["payloads"] = company(steady_years(**overrides))
        return annual_metrics(fin.load_financials("TEST", "2026-09-17"))

    def test_roic_and_rotc_by_hand(self, av):
        last = self.metrics(av).iloc[-1]
        assert last.roic == pytest.approx(0.20)
        assert last.rotc == pytest.approx(200 / 600)
        assert last.roe == pytest.approx(190 / 800)
        assert last.fcf == pytest.approx(200)
        assert last.fcf_conversion == pytest.approx(200 / 190)

    def test_operating_income_is_used_not_av_ebit(self, av):
        """AV's ebit includes non-operating gains; returns must not move with it."""
        assert self.metrics(av, ebit=5000).iloc[-1].roic == pytest.approx(0.20)

    def test_negative_equity_gives_no_roe_rather_than_a_negative_one(self, av):
        assert math.isnan(self.metrics(av, totalShareholderEquity=-100).iloc[-1].roe)

    def test_buybacks_read_from_negative_proceeds(self, av):
        assert self.metrics(av).iloc[-1].buybacks == pytest.approx(30)

    def test_loss_year_tax_rate_falls_back_to_the_median(self, av):
        years = steady_years()
        years["2025-12-31"] = {**STEADY, "incomeBeforeTax": -10, "incomeTaxExpense": 5}
        av["payloads"] = company(years)
        # Median rate of the other years is 20%, so NOPAT is still 250 x 0.8.
        assert annual_metrics(fin.load_financials("TEST", "2026-09-17")).iloc[-1].roic == pytest.approx(0.20)

    def test_cagr(self):
        s = pd.Series([100.0, 110.0, 121.0], index=pd.date_range("2023", periods=3, freq="YE"))
        assert cagr(s, 2) == pytest.approx(0.10)
        assert math.isnan(cagr(s, 5))
        assert math.isnan(cagr(pd.Series([-1.0, 5.0, 9.0], index=s.index), 2))

    def test_trend_per_year(self):
        s = pd.Series([0.10, 0.11, 0.12, 0.13, 0.14], index=pd.date_range("2021", periods=5, freq="YE"))
        assert trend_per_year(s) == pytest.approx(0.01, abs=1e-4)
        assert math.isnan(trend_per_year(s.iloc[:3]))


class TestQualityScreens:

    def screens(self, av, ttm=float("nan"), **overrides):
        av["payloads"] = company(steady_years(**overrides))
        m = annual_metrics(fin.load_financials("TEST", "2026-09-17"))
        return {s.name.split(":")[0].split(" ")[0]: s for s in quality_screens(m, ttm_cash_conversion=ttm)}

    def test_healthy_business_clears_everything(self, av):
        assert {s.status for s in self.screens(av).values()} == {"CLEAR"}

    def test_poor_returns_trip(self, av):
        assert self.screens(av, operatingIncome=50, incomeBeforeTax=50, incomeTaxExpense=10)["Returns"].status == "TRIPPED"

    def test_leverage_trips(self, av):
        assert self.screens(av, shortLongTermDebtTotal=2000)["Leverage"].status == "TRIPPED"

    def test_net_cash_clears_leverage_even_without_interest(self, av):
        s = self.screens(av, shortLongTermDebtTotal=0, interestExpense=0)["Leverage"]
        assert s.status == "CLEAR"

    def test_persistent_cash_shortfall_trips(self, av):
        s = self.screens(av, ttm=0.5, operatingCashflow=100)["Accounting"]
        assert s.status == "TRIPPED"

    def test_episodic_cash_shortfall_is_watch_not_tripped(self, av):
        """One-off outflows drag the annual figures; the trailing year has recovered."""
        s = self.screens(av, ttm=1.1, operatingCashflow=100)["Accounting"]
        assert s.status == "WATCH"
        assert "one-off" in s.evidence

    def test_receivables_outgrowing_revenue_trips_regardless_of_cash(self, av):
        years = steady_years()
        for i, y in enumerate(range(2022, 2026)):
            years[f"{y}-12-31"]["currentNetReceivables"] = 100 * 1.2 ** i
        av["payloads"] = company(years)
        m = annual_metrics(fin.load_financials("TEST", "2026-09-17"))
        acct = [s for s in quality_screens(m, ttm_cash_conversion=1.2) if s.name.startswith("Accounting")][0]
        assert acct.status == "TRIPPED"


# --- valuation ------------------------------------------------------------------


class TestDcf:

    def test_zero_growth_matches_closed_form(self):
        base, r, gt, n = 100.0, 0.08, 0.025, 10
        annuity = base * (1 - (1 + r) ** -n) / r
        terminal = base * (1 + gt) / (r - gt) / (1 + r) ** n
        assert val.dcf_value(base, 0.0, r, gt, n) == pytest.approx(annuity + terminal)

    @pytest.mark.parametrize("growth", [-0.05, 0.0, 0.04, 0.12])
    def test_reverse_dcf_inverts_the_dcf(self, growth):
        value = val.dcf_value(100.0, growth)
        assert val.reverse_dcf(value, 100.0) == pytest.approx(growth, abs=1e-5)

    def test_reverse_dcf_is_undefined_for_non_positive_cash_flow(self):
        assert math.isnan(val.reverse_dcf(1000.0, -5.0))
        assert math.isnan(val.reverse_dcf(1000.0, 0.0))

    def test_reverse_dcf_clamps_at_the_search_range(self):
        assert val.reverse_dcf(1e12, 1.0) == pytest.approx(1.0)
        assert val.reverse_dcf(1.0, 1000.0) == pytest.approx(-0.5)

    def test_discount_rate_must_exceed_terminal_growth(self):
        with pytest.raises(ValueError):
            val.dcf_value(100.0, 0.0, discount_rate=0.02, terminal_growth=0.03)

    def test_percentile_in_history(self):
        h = pd.Series([10.0, 20.0, 30.0, 40.0])
        assert val.percentile_in_history(25.0, h) == 0.5
        assert math.isnan(val.percentile_in_history(float("nan"), h))


class TestMarginOfSafetyScreen:

    def test_tripped_when_every_base_demands_more_than_the_record(self):
        s = val.margin_of_safety_screen({"normalized": 0.12, "trailing": 0.10}, 0.04, "10y revenue CAGR")
        assert s.status == "TRIPPED"

    def test_watch_when_the_bases_disagree(self):
        s = val.margin_of_safety_screen({"normalized": 0.094, "trailing": 0.044}, 0.047, "x")
        assert s.status == "WATCH"

    def test_clear_within_tolerance(self):
        s = val.margin_of_safety_screen({"normalized": 0.05, "trailing": 0.06}, 0.047, "x")
        assert s.status == "CLEAR"

    def test_no_data(self):
        s = val.margin_of_safety_screen({"normalized": float("nan")}, 0.04, "x")
        assert s.status == "NO DATA"

    def test_judged_at_the_generous_discount_rate(self):
        assert min(val.DISCOUNT_RATE_GRID) == val.SCREEN_DISCOUNT_RATE


class TestValuationFromStatements:

    @pytest.fixture
    def loaded(self, av):
        av["payloads"] = company(steady_years(), quarter_rows(), reported={})
        f = fin.load_financials("TEST", "2026-09-17")
        idx = pd.date_range("2013-12-20", "2026-09-17", freq="D")
        prices = pd.Series(20.0, index=idx)
        return f, prices

    def test_snapshot_enterprise_value(self, loaded):
        f, prices = loaded
        s = val.snapshot(f, prices)
        assert s.market_cap == pytest.approx(20.0 * 100)
        assert s.enterprise_value == pytest.approx(2000 + 400 - 200)
        assert s.ttm_fcf == pytest.approx(200)
        assert s.ev_to_ebit == pytest.approx(2200 / 250)
        assert s.pe == pytest.approx(2000 / 190)

    def test_snapshot_needs_a_recent_price(self, loaded):
        f, _ = loaded
        stale = pd.Series([20.0], index=[pd.Timestamp("2026-08-01")])
        assert val.snapshot(f, stale) is None

    def test_historical_multiples_use_fiscal_year_end_close(self, loaded):
        f, prices = loaded
        h = val.historical_multiples(f, prices)
        assert h["ev_to_ebit"].iloc[-1] == pytest.approx((20.0 * 100 + 200) / 250)

    def test_growth_record_includes_operating_income(self, av):
        """A refranchiser: revenue shrinks while operating income compounds."""
        years = {}
        for i, y in enumerate(range(2014, 2026)):
            years[f"{y}-12-31"] = {**STEADY, "totalRevenue": 1000 * 0.98 ** i,
                                   "operatingIncome": 250 * 1.05 ** i}
        av["payloads"] = company(years)
        rate, label = val.growth_record(fin.load_financials("TEST", "2026-09-17"))
        assert "operating-income" in label
        assert rate == pytest.approx(0.05, abs=1e-6)

    @staticmethod
    def _hypergrowth():
        """TSLA-shaped: revenue compounds 37%/yr from a small base; the business
        lost money early and its profit has compounded far more slowly since."""
        years = {}
        for i, y in enumerate(range(2014, 2026)):
            profitable = y >= 2019
            years[f"{y}-12-31"] = {
                **STEADY, "totalRevenue": 100 * 1.37 ** i,
                "operatingIncome": 250 * 1.17 ** (y - 2019) if profitable else -50,
                "operatingCashflow": 240 * 1.18 ** (y - 2019) if profitable else -50,
                "capitalExpenditures": 40 * 1.18 ** (y - 2019),
            }
        return years

    def test_growth_record_ignores_revenue_that_outran_profit(self, av):
        av["payloads"] = company(self._hypergrowth())
        rate, label = val.growth_record(fin.load_financials("TEST", "2026-09-17"))
        assert "revenue" not in label
        assert label.startswith("5y ")  # the 10y window starts in a loss year
        assert rate == pytest.approx(0.18, abs=1e-6)

    def test_hypergrowth_price_trips_the_screen(self, av):
        """The TSLA case: 32-36% implied FCF growth against an 18% profit record."""
        av["payloads"] = company(self._hypergrowth())
        rate, label = val.growth_record(fin.load_financials("TEST", "2026-09-17"))
        s = val.margin_of_safety_screen({"normalized": 0.359, "trailing": 0.320}, rate, label)
        assert s.status == "TRIPPED"

    def test_growth_record_undefined_without_profit(self, av):
        av["payloads"] = company(steady_years(operatingIncome=-10, operatingCashflow=-10))
        rate, label = val.growth_record(fin.load_financials("TEST", "2026-09-17"))
        assert math.isnan(rate)
        assert "profit" in label


# --- LLM tools ------------------------------------------------------------------


class TestTools:

    @pytest.fixture
    def served(self, av):
        av["payloads"] = company(steady_years(), quarter_rows())
        prices = pd.Series(20.0, index=pd.date_range("2013-12-20", "2026-09-17", freq="D"))
        with patch.object(vt, "price_history", return_value=prices):
            yield

    @pytest.mark.parametrize("tool, sections", [
        (vt.get_quality_metrics, ["Annual history", "Quarterly cash flow", "Screens", "ROTC"]),
        (vt.get_capital_allocation, ["Where the operating cash went", "By year", "Dividends"]),
        (vt.get_valuation_history, ["Now vs the company's own history", "Percentile"]),
        (vt.get_reverse_dcf, ["FCF growth per year the price requires", "margin of safety", "Screens"]),
    ])
    def test_each_tool_renders_its_sections(self, served, tool, sections):
        out = tool.invoke({"ticker": "TEST", "curr_date": "2026-09-17"})
        assert not out.startswith("UNAVAILABLE"), out
        for section in sections:
            assert section in out
        assert "Do not recompute ratios" in out

    def test_failures_become_an_explicit_unavailable_notice(self):
        with patch.object(fin, "_fetch", side_effect=RuntimeError("rate limit")):
            out = vt.get_quality_metrics.invoke({"ticker": "TEST", "curr_date": "2026-09-17"})
        assert out.startswith("UNAVAILABLE")
        assert "Do not substitute figures from memory" in out

    def test_quality_and_valuation_tools_are_disjoint(self):
        quality = {t.name for t in vt.QUALITY_TOOLS}
        valuation = {t.name for t in vt.VALUATION_TOOLS}
        assert quality and valuation and not quality & valuation
