"""LEAPS decision layer: cost measures, the four screens, and the tool report."""

from __future__ import annotations

import math
from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.mandates.tools import leaps as lp, leaps_tools as lt, options as op


def _priced(strike=100.0, bid=19.8, ask=20.2, oi=500, s=100.0, years=1.5, r=0.04, q=0.0, vol=0.35):
    c = op.Contract("C", pd.Timestamp("2025-09-02") + pd.Timedelta(days=round(years * 365)),
                    strike, "call", bid, ask, oi, 10)
    return op.price_contract(c, s, "2025-09-02", r, q)


def _view(p, realised=0.33, horizon=126):
    return lp.LeapsView(underlying=100.0, rate=0.04, dividend_yield=0.0, realised_vol=realised,
                        horizon_days=horizon, contract=p, comparison=None)


def _named(screens):
    return {s.name: s for s in screens}


class TestCostMeasures:
    def test_realised_vol_of_a_known_series(self):
        """Alternating +1%/-1% days: daily log-return std of ~1%, ~15.9% a year."""
        steps = [1.01, 1 / 1.01] * 150
        close = pd.Series(100.0 * pd.Series(steps).cumprod().values,
                          index=pd.bdate_range("2024-01-01", periods=300))
        assert lp.realised_vol(close, "2025-12-31") == pytest.approx(0.01 * math.sqrt(252), rel=0.02)

    def test_realised_vol_needs_history(self):
        close = pd.Series([100.0] * 20, index=pd.bdate_range("2025-01-01", periods=20))
        assert math.isnan(lp.realised_vol(close, "2025-12-31"))

    def test_breakeven_is_the_move_that_returns_the_ask_net_of_the_spread(self):
        p = _priced(bid=19.8, ask=20.2)
        be = lp.horizon_breakeven(p, 126)
        c = p.contract
        remaining = p.years - 126 / 252
        exit_value = op.bsm_call(100 * (1 + be), c.strike, remaining, p.rate, 0.0, p.iv) * c.bid / c.mid
        assert exit_value == pytest.approx(c.ask, rel=1e-4)
        assert be > 0  # time decay and the spread both have to be earned back

    def test_a_wider_spread_needs_a_bigger_move(self):
        tight = lp.horizon_breakeven(_priced(bid=19.9, ask=20.1), 126)
        wide = lp.horizon_breakeven(_priced(bid=18.5, ask=21.5), 126)
        assert wide > tight

    def test_no_breakeven_past_expiry(self):
        assert math.isnan(lp.horizon_breakeven(_priced(years=0.3), 126))


class TestScreens:
    def test_no_contract_trips_the_expiry_screen_alone(self):
        (s,) = lp.leaps_screens(_view(None))
        assert (s.name, s.status) == ("No expiry long enough", "TRIPPED")

    def test_a_liquid_fairly_priced_call_clears(self):
        s = _named(lp.leaps_screens(_view(_priced())))
        assert {x.status for x in s.values()} == {"CLEAR"}

    def test_thin_open_interest_is_illiquid(self):
        s = _named(lp.leaps_screens(_view(_priced(oi=20))))
        assert s["Illiquid"].status == "TRIPPED"

    def test_implied_far_above_realised_is_expensive(self):
        p = _priced()
        s = _named(lp.leaps_screens(_view(p, realised=p.iv / 1.5)))
        assert s["Expensive volatility"].status == "TRIPPED"

    def test_no_realised_vol_cannot_settle_the_volatility_screen(self):
        s = _named(lp.leaps_screens(_view(_priced(), realised=float("nan"))))
        assert s["Expensive volatility"].status == "NO DATA"

    def test_a_break_even_beyond_half_a_sigma_is_too_costly(self):
        """A far out-of-the-money call on a quiet stock: most of the premium is time value."""
        p = _priced(strike=130.0, bid=0.95, ask=1.35, vol=0.12)
        s = _named(lp.leaps_screens(_view(p, realised=p.iv)))
        assert s["Time value too costly"].status == "TRIPPED"


class TestTool:
    def _view(self):
        judged = _priced()
        return lp.LeapsView(100.0, 0.04, 0.02, 0.33, 126, judged, _priced(strike=110.0, bid=14.8, ask=15.2))

    def test_report_names_the_rule_the_screens_and_both_contracts(self):
        with patch.object(lp, "leaps_view", return_value=self._view()):
            out = lt.get_leaps_candidates.invoke({"ticker": "X", "curr_date": "2025-09-02"})
        assert "selected by rule" in out
        assert "Judged (delta 0.75)" in out and "graded alongside" in out
        assert "### Screens (LEAPS" in out
        assert "do not choose a different strike or expiry" in out

    def test_a_data_failure_is_a_notice_not_a_crash(self):
        with patch.object(lp, "leaps_view", side_effect=RuntimeError("vendor down")):
            out = lt.get_leaps_candidates.invoke({"ticker": "X", "curr_date": "2025-09-02"})
        assert out.startswith("UNAVAILABLE")

    def test_no_contract_renders_as_gaps(self):
        v = lp.LeapsView(100.0, 0.04, 0.0, 0.33, 126, None, None)
        with patch.object(lp, "leaps_view", return_value=v):
            out = lt.get_leaps_candidates.invoke({"ticker": "X", "curr_date": "2025-09-02"})
        assert "**TRIPPED** -- No expiry long enough" in out
