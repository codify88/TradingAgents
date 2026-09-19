"""P3 deterministic momentum tools: price/trend maths, growth, estimate revisions.

Price series are synthetic and chosen so every expected figure can be worked out
by hand; the estimate payloads are shaped exactly like Alpha Vantage's (numbers
as strings) so the parser is exercised end to end.
"""

import json
import math
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from tradingagents.mandates.tools import growth as gr, momentum as mo


def _frame(closes, volumes=None, start="2025-01-01"):
    """Daily OHLCV with a 1% intraday range, indexed on business days."""
    idx = pd.bdate_range(start, periods=len(closes))
    close = pd.Series([float(c) for c in closes], index=idx)
    return pd.DataFrame({
        "Open": close,
        "High": close * 1.005,
        "Low": close * 0.995,
        "Close": close,
        "Volume": pd.Series(
            [float(v) for v in (volumes if volumes is not None else [1e6] * len(closes))],
            index=idx,
        ),
    })


def _ramp(n, start=100.0, step=0.1):
    return [start + i * step for i in range(n)]


# Calibrated to real proportions: this produces an ATR around 2.5% of price and
# a 50-day average roughly 1.4 ATRs below it -- the same shape a real trending
# large-cap shows. A noiseless ramp has almost no true range, so its 50-day
# average sits a dozen ATRs below price and every candidate stop reads as
# undefendable, which is an artefact of the fixture rather than of the maths.
TREND_DRIFT, TREND_NOISE, TREND_SEED = 0.0015, 0.020, 3
# A separate draw for the falling case: a seed that trends cleanly up is not
# the same one that trends cleanly down.
DOWNTREND_SEED = 0


def _trend(n, start=100.0, drift=TREND_DRIFT, noise=TREND_NOISE, seed=TREND_SEED):
    """A random walk with drift, with an ATR realistic against the trend."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, noise, n)
    return list(start * np.exp(np.cumsum(steps)))


# --- trailing returns and relative strength --------------------------------


class TestTrailingReturns:
    def test_return_is_measured_over_the_right_number_of_bars(self):
        # 300 bars rising 1.0 each: the 21-bar return is 21 / price-21-bars-back.
        prices = pd.Series(_ramp(300, 100.0, 1.0), index=pd.bdate_range("2025-01-01", periods=300))
        out = mo.trailing_returns(prices)
        assert out["1m"] == pytest.approx(21 / (399 - 21), rel=1e-6)
        assert out["12m"] == pytest.approx(252 / (399 - 252), rel=1e-6)

    def test_a_window_longer_than_the_history_is_not_guessed(self):
        prices = pd.Series(_ramp(30), index=pd.bdate_range("2025-01-01", periods=30))
        out = mo.trailing_returns(prices)
        assert not math.isnan(out["1m"])
        assert math.isnan(out["6m"]) and math.isnan(out["12m"])

    def test_twelve_one_skips_the_most_recent_month(self):
        """The skipped month is the definition: short-horizon reversal runs the
        other way, so including it dilutes the signal being measured."""
        prices = pd.Series(_ramp(300, 100.0, 1.0), index=pd.bdate_range("2025-01-01", periods=300))
        # Spike the last 21 bars: 12-1 must ignore it, plain 12m must not.
        spiked = prices.copy()
        spiked.iloc[-21:] = spiked.iloc[-21:] * 2
        assert mo.momentum_12_1(spiked) == pytest.approx(mo.momentum_12_1(prices))
        assert mo.trailing_returns(spiked)["12m"] > mo.trailing_returns(prices)["12m"] * 1.5

    def test_excess_return_aligns_dates_before_differencing(self):
        """A holiday one venue observes and the other does not would otherwise
        shift the two windows against each other."""
        idx = pd.bdate_range("2025-01-01", periods=300)
        stock = pd.Series(_ramp(300, 100.0, 1.0), index=idx)
        bench = pd.Series(_ramp(300, 100.0, 1.0), index=idx).drop(idx[50])
        out = mo.excess_returns(stock, bench)
        assert out["1m"] == pytest.approx(0.0, abs=1e-9)

    def test_no_overlap_gives_no_answer(self):
        a = pd.Series(_ramp(30), index=pd.bdate_range("2025-01-01", periods=30))
        b = pd.Series(_ramp(30), index=pd.bdate_range("2030-01-01", periods=30))
        assert all(math.isnan(v) for v in mo.excess_returns(a, b).values())


# --- trend structure -------------------------------------------------------


class TestTrendStructure:
    def test_an_uptrend_reads_as_one(self):
        t = mo.trend_structure(_frame(_trend(300)))
        assert t.price > t.sma50 > t.sma200
        assert t.sma50_rising and t.sma200_rising
        # Near the high, not pinned to it: a real uptrend breathes.
        assert t.pct_from_high > -0.15
        assert t.pct_above_low > 0.5

    def test_a_downtrend_reads_as_one(self):
        t = mo.trend_structure(_frame(_trend(300, 250.0, drift=-TREND_DRIFT, seed=DOWNTREND_SEED)))
        assert t.price < t.sma50 < t.sma200
        assert not t.sma50_rising and not t.sma200_rising
        assert t.pct_from_high < -0.3

    def test_volume_on_advances_versus_declines(self):
        closes = [100 + (1 if i % 2 else -1) for i in range(120)]
        volumes = [2e6 if i % 2 else 1e6 for i in range(120)]  # up days carry twice
        t = mo.trend_structure(_frame(closes, volumes))
        assert t.up_down_volume > 1.5

    def test_atr_tracks_the_bar_range(self):
        # 1% range on a ~100 price: ATR should land near 1.0, not 0 or 100.
        t = mo.trend_structure(_frame([100.0] * 120))
        assert 0.5 < t.atr14 < 2.0

    def test_volatility_is_not_reported_from_too_few_bars(self):
        assert math.isnan(mo.annualized_volatility(pd.Series(_ramp(5))))

    def test_swing_low_finds_the_last_local_low_not_the_minimum(self):
        """The stop a trend-follower watches is the most recent higher low, not
        the lowest point of the whole window."""
        closes = _ramp(40, 100.0, -1.0) + _ramp(40, 60.0, 1.0) + [100 - i for i in range(11)] + _ramp(30, 89.0, 1.0)
        t = mo.trend_structure(_frame(closes))
        assert t.swing_low > 60.0


class TestInvalidationLevels:
    def test_levels_above_the_price_are_not_offered_as_stops(self):
        t = mo.trend_structure(_frame(_trend(300, 250.0, drift=-TREND_DRIFT, seed=DOWNTREND_SEED)))
        assert all(level < t.price for _, level, _ in mo.invalidation_levels(t))

    def test_distance_is_expressed_in_atrs(self):
        t = mo.trend_structure(_frame(_trend(300)))
        levels = mo.invalidation_levels(t)
        assert levels
        for _, level, atrs in levels:
            assert atrs == pytest.approx((t.price - level) / t.atr14, rel=1e-6)

    def test_nearest_level_is_first(self):
        t = mo.trend_structure(_frame(_trend(300)))
        levels = mo.invalidation_levels(t)
        assert levels == sorted(levels, key=lambda r: -r[1])


# --- price screens ---------------------------------------------------------


def _screens(closes, vs_bench):
    frame = _frame(closes)
    t = mo.trend_structure(frame)
    return {s.name: s for s in mo.momentum_screens(t, mo.trailing_returns(frame["Close"]), vs_bench)}


class TestMomentumScreens:
    def test_a_healthy_uptrend_clears_everything(self):
        s = _screens(_trend(300), {"6m": 0.08, "12m": 0.15})
        assert all(v.status == "CLEAR" for v in s.values()), {k: v.status for k, v in s.items()}

    def test_a_broken_trend_is_tripped(self):
        s = _screens(_trend(300, 250.0, drift=-TREND_DRIFT, seed=DOWNTREND_SEED), {"6m": -0.1, "12m": -0.2})
        assert s["Trend broken: price below a falling 50-day average"].status == "TRIPPED"

    def test_lagging_the_benchmark_over_both_windows_is_tripped(self):
        s = _screens(_trend(300), {"6m": -0.05, "12m": -0.02})
        assert s["Lags its benchmark over both the 6- and 12-month windows"].status == "TRIPPED"

    def test_lagging_one_window_only_is_not(self):
        s = _screens(_trend(300), {"6m": -0.05, "12m": 0.10})
        assert s["Lags its benchmark over both the 6- and 12-month windows"].status == "CLEAR"

    def test_a_missing_benchmark_is_no_data_not_a_pass(self):
        s = _screens(_trend(300), {})
        assert s["Lags its benchmark over both the 6- and 12-month windows"].status == "NO DATA"

    def test_a_stop_too_far_away_is_not_protection(self):
        """A quiet stock in a fast trend leaves every level many ATRs below the
        price. The stop is then a formality: the loss it permits is the whole
        thesis, so the screen trips rather than accepting it."""
        t = mo.trend_structure(_frame(_ramp(300, 100.0, 0.5)))  # noiseless: tiny ATR
        assert mo.invalidation_levels(t)[0][2] > mo.MAX_STOP_ATR
        screens = {s.name: s for s in mo.momentum_screens(
            t, {"6m": 0.1, "12m": 0.2}, {"6m": 0.05, "12m": 0.05})}
        assert screens["No defensible invalidation level"].status == "TRIPPED"


# --- growth ----------------------------------------------------------------


class _Fin:
    """Minimal stand-in for Financials: only .col is needed by growth_trajectory."""

    def __init__(self, annual, quarterly):
        self.annual, self.quarterly = annual, quarterly

    def col(self, name, fallbacks=(), quarterly=False):
        df = self.quarterly if quarterly else self.annual
        if name in df:
            return df[name].astype(float)
        for fb in fallbacks:
            if fb in df:
                return df[fb].astype(float)
        return pd.Series(np.nan, index=df.index, dtype=float)


def _fin(annual_rows, quarterly_rows):
    return _Fin(
        pd.DataFrame(annual_rows, index=pd.to_datetime([r.pop("end") for r in annual_rows.copy()])
                     if False else pd.to_datetime([r["end"] for r in annual_rows])).drop(columns=["end"]),
        pd.DataFrame(quarterly_rows, index=pd.to_datetime([r["end"] for r in quarterly_rows])).drop(columns=["end"]),
    )


class TestGrowthTrajectory:
    def test_accelerating_growth_is_positive(self):
        """YoY growth rising from 10% to 30% over four quarters is +20pp.

        Nine quarters, because acceleration compares the latest year-on-year
        rate with the rate four quarters before it, and each of those needs its
        own year-ago base.
        """
        ends = pd.date_range("2024-03-31", periods=9, freq="QE")
        revenue = [100, 100, 100, 100, 110, 115, 122, 130, 143]
        quarterly = pd.DataFrame(
            {"totalRevenue": revenue,
             "netIncome": [r / 10 for r in revenue],
             "commonStockSharesOutstanding": [100] * 9},
            index=ends,
        )
        annual = pd.DataFrame(
            {"totalRevenue": [380, 420], "netIncome": [38, 42], "grossProfit": [200, 230],
             "operatingIncome": [80, 95], "operatingCashflow": [50, 60],
             "capitalExpenditures": [10, 12], "commonStockSharesOutstanding": [100, 100]},
            index=pd.to_datetime(["2024-12-31", "2025-12-31"]),
        )
        g = gr.growth_trajectory(_Fin(annual, quarterly))
        assert g.quarterly_revenue_growth.dropna().iloc[-1] == pytest.approx(0.30)
        assert g.revenue_acceleration == pytest.approx(20.0, abs=0.01)

    def test_growth_on_a_negative_base_is_not_reported(self):
        """Earnings crossing zero produce percentages that look spectacular and
        mean nothing."""
        ends = pd.date_range("2024-03-31", periods=8, freq="QE")
        quarterly = pd.DataFrame(
            {"totalRevenue": [100] * 8,
             "netIncome": [-10, -10, -10, -10, 20, 20, 20, 20],
             "commonStockSharesOutstanding": [100] * 8},
            index=ends,
        )
        annual = pd.DataFrame(
            {"totalRevenue": [400, 400], "netIncome": [-40, 80], "grossProfit": [200, 200],
             "operatingIncome": [50, 50], "operatingCashflow": [50, 50],
             "capitalExpenditures": [10, 10], "commonStockSharesOutstanding": [100, 100]},
            index=pd.to_datetime(["2024-12-31", "2025-12-31"]),
        )
        g = gr.growth_trajectory(_Fin(annual, quarterly))
        assert g.quarterly_eps_growth.dropna().empty
        assert g.annual_eps_growth.dropna().empty

    def test_eps_is_per_share_so_buybacks_are_not_growth(self):
        ends = pd.date_range("2024-03-31", periods=8, freq="QE")
        quarterly = pd.DataFrame(
            {"totalRevenue": [100] * 8,
             "netIncome": [10] * 8,
             # Half the shares retired: net income flat, EPS doubles.
             "commonStockSharesOutstanding": [100, 100, 100, 100, 50, 50, 50, 50]},
            index=ends,
        )
        annual = pd.DataFrame(
            {"totalRevenue": [400], "netIncome": [40], "grossProfit": [200],
             "operatingIncome": [50], "operatingCashflow": [50],
             "capitalExpenditures": [10], "commonStockSharesOutstanding": [100]},
            index=pd.to_datetime(["2024-12-31"]),
        )
        g = gr.growth_trajectory(_Fin(annual, quarterly))
        assert g.quarterly_eps_growth.dropna().iloc[-1] == pytest.approx(1.0)
        assert g.quarterly_revenue_growth.dropna().iloc[-1] == pytest.approx(0.0)


# --- estimate revisions ----------------------------------------------------


def _estimates(**over):
    row = {
        "date": "2099-12-31", "horizon": "fiscal year",
        "eps_estimate_average": "10.0", "eps_estimate_analyst_count": "20",
        "eps_estimate_average_30_days_ago": "8.0",
        "eps_estimate_average_90_days_ago": "5.0",
        "revenue_estimate_average": "1000",
        "eps_estimate_revision_up_trailing_30_days": "18",
        "eps_estimate_revision_down_trailing_30_days": "2",
    }
    row.update(over)
    return json.dumps({"symbol": "X", "estimates": [row]})


class TestEstimateRevisions:
    def test_drift_and_breadth_by_hand(self):
        with patch.object(gr, "_fetch", return_value=_estimates()):
            r = gr.estimate_revisions("X", pd.Timestamp.today())[0]
        assert r.drift_30d == pytest.approx(0.25)      # 10 vs 8
        assert r.drift_90d == pytest.approx(1.0)       # 10 vs 5
        assert r.breadth_30d == pytest.approx(0.8)     # (18-2)/20

    def test_all_downward_revisions_give_breadth_of_minus_one(self):
        payload = _estimates(**{
            "eps_estimate_revision_up_trailing_30_days": "0",
            "eps_estimate_revision_down_trailing_30_days": "12",
        })
        with patch.object(gr, "_fetch", return_value=payload):
            assert gr.estimate_revisions("X", pd.Timestamp.today())[0].breadth_30d == pytest.approx(-1.0)

    def test_no_revisions_is_not_read_as_neutral_breadth(self):
        payload = _estimates(**{
            "eps_estimate_revision_up_trailing_30_days": "0",
            "eps_estimate_revision_down_trailing_30_days": "0",
        })
        with patch.object(gr, "_fetch", return_value=payload):
            assert math.isnan(gr.estimate_revisions("X", pd.Timestamp.today())[0].breadth_30d)

    def test_a_period_already_ended_is_not_a_forecast(self):
        with patch.object(gr, "_fetch", return_value=_estimates(date="2020-12-31")):
            assert gr.estimate_revisions("X", pd.Timestamp.today()) == []

    def test_a_historical_run_is_not_served_todays_consensus(self):
        """Estimates carry no as-of date, so a backtest must not read them."""
        with patch.object(gr, "_fetch", return_value=_estimates()) as fetch:
            assert gr.estimate_revisions("X", pd.Timestamp("2020-01-02")) == []
        fetch.assert_not_called()

    def test_a_run_dated_within_tolerance_is_served(self):
        """A run on the last trading day is the normal case, and a few days is
        immaterial against 30- and 90-day revision windows."""
        as_of = pd.Timestamp.today().normalize() - pd.Timedelta(days=mo.WINDOWS["1m"] // 21)
        with patch.object(gr, "_fetch", return_value=_estimates()):
            assert gr.estimate_revisions("X", as_of)


# --- fundamental screens ---------------------------------------------------


def _traj(latest_growth, acceleration_pp, ttm_growth=float("nan")):
    """A trajectory with a chosen latest quarterly growth and acceleration."""
    earlier = latest_growth - acceleration_pp / 100
    idx = pd.date_range("2024-03-31", periods=5, freq="QE")
    return gr.GrowthTrajectory(
        annual_revenue_growth=pd.Series(dtype=float),
        annual_eps_growth=pd.Series([0.1], index=pd.to_datetime(["2025-12-31"])),
        quarterly_revenue_growth=pd.Series([earlier, 0.0, 0.0, 0.0, latest_growth], index=idx),
        quarterly_eps_growth=pd.Series(dtype=float),
        gross_margin=pd.Series(dtype=float), operating_margin=pd.Series(dtype=float),
        share_count=pd.Series(dtype=float), fcf=pd.Series(dtype=float),
        net_issuance=pd.Series(dtype=float),
        ttm_revenue_growth=ttm_growth,
    )


def _named(screens):
    return {s.name: s for s in screens}


class TestGrowthScreens:
    def test_deceleration_with_an_expanding_multiple_is_tripped(self):
        s = _named(gr.growth_screens(_traj(0.10, -20.0), [], True, 0.3))
        assert s["Growth decelerating while the multiple is still expanding"].status == "TRIPPED"

    def test_deceleration_alone_is_a_watch_not_a_pass(self):
        """It is half the disqualifier, and the half that usually arrives first."""
        s = _named(gr.growth_screens(_traj(0.10, -20.0), [], False, 0.3))
        assert s["Growth decelerating while the multiple is still expanding"].status == "WATCH"

    def test_acceleration_clears_it(self):
        s = _named(gr.growth_screens(_traj(0.30, +20.0), [], True, 0.3))
        assert s["Growth decelerating while the multiple is still expanding"].status == "CLEAR"

    def test_price_up_while_estimates_are_cut_is_the_divergence(self):
        with patch.object(gr, "_fetch", return_value=_estimates(**{
            "eps_estimate_revision_up_trailing_30_days": "1",
            "eps_estimate_revision_down_trailing_30_days": "19",
        })):
            revisions = gr.estimate_revisions("X", pd.Timestamp.today())
        s = _named(gr.growth_screens(_traj(0.30, 5.0), revisions, False, 0.4))
        assert s["Price momentum and fundamental momentum point in opposite directions"].status == "TRIPPED"

    def test_rising_estimates_alongside_a_rising_price_is_agreement(self):
        with patch.object(gr, "_fetch", return_value=_estimates()):
            revisions = gr.estimate_revisions("X", pd.Timestamp.today())
        s = _named(gr.growth_screens(_traj(0.30, 5.0), revisions, False, 0.4))
        assert s["Price momentum and fundamental momentum point in opposite directions"].status == "CLEAR"

    def test_no_estimates_cannot_settle_the_divergence_screen(self):
        s = _named(gr.growth_screens(_traj(0.30, 5.0), [], False, 0.4))
        assert s["Price momentum and fundamental momentum point in opposite directions"].status == "NO DATA"

    def test_a_low_growth_business_is_not_a_growth_candidate(self):
        s = _named(gr.growth_screens(_traj(0.01, 1.0, ttm_growth=0.02), [], False, 0.1))
        assert s["Not actually a growth business"].status == "TRIPPED"

    def test_one_weak_quarter_in_a_growing_year_is_a_watch(self):
        """TSLA-shaped timing: the quarter is below the floor, the year is not."""
        s = _named(gr.growth_screens(_traj(-0.118, -14.1, ttm_growth=0.08), [], False, 0.6))
        assert s["Not actually a growth business"].status == "WATCH"

    def test_one_strong_quarter_does_not_rescue_a_flat_year(self):
        s = _named(gr.growth_screens(_traj(0.09, 5.0, ttm_growth=0.01), [], False, 0.1))
        assert s["Not actually a growth business"].status == "WATCH"

    def test_a_single_quarter_alone_never_trips(self):
        s = _named(gr.growth_screens(_traj(0.01, 1.0), [], False, 0.1))
        assert s["Not actually a growth business"].status == "WATCH"

    def test_growth_on_both_readings_clears(self):
        s = _named(gr.growth_screens(_traj(0.20, 1.0, ttm_growth=0.18), [], False, 0.1))
        assert s["Not actually a growth business"].status == "CLEAR"


class TestTrailingYearGrowth:
    def test_trailing_four_quarters_against_the_four_before(self):
        q = pd.Series([100.0] * 4 + [110.0] * 4, index=pd.date_range("2024-03-31", periods=8, freq="QE"))
        assert gr._ttm_growth(q) == pytest.approx(0.10)

    def test_needs_eight_quarters(self):
        q = pd.Series([100.0] * 7, index=pd.date_range("2024-03-31", periods=7, freq="QE"))
        assert math.isnan(gr._ttm_growth(q))


# --- tool wrappers ---------------------------------------------------------


class TestToolSafety:
    def test_a_data_failure_becomes_a_notice_not_a_crash(self):
        from tradingagents.mandates.tools import momentum_tools as mt

        with patch.object(mt, "_series", side_effect=RuntimeError("vendor down")):
            out = mt._relative_strength_report("X", "2026-09-17")
        assert out.startswith("UNAVAILABLE")
        assert "do not substitute figures from memory" in out.lower()

    def test_every_momentum_tool_is_guarded(self):
        from tradingagents.mandates.tools import momentum_tools as mt

        for fn in (mt._relative_strength_report, mt._trend_structure_report,
                   mt._growth_trajectory_report, mt._estimate_revisions_report):
            assert hasattr(fn, "__wrapped__"), f"{fn.__name__} is not guarded"
