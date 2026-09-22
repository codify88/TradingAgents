"""LEAPS data layer: our own Black-Scholes-Merton pricing, point-in-time
chains, rates and dividends, and the contract-selection rule."""

from __future__ import annotations

import math
from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.mandates.tools import options as op


@pytest.fixture(autouse=True)
def _clear_caches():
    for fn in (op.option_chain, op._treasury_series, op._dividends):
        fn.cache_clear()
    yield


# --- pricing ------------------------------------------------------------------


class TestBlackScholesMerton:
    def test_matches_the_textbook_call(self):
        """Hull, Options, Futures and Other Derivatives, Example 15.6:
        S=42, K=40, r=10%, sigma=20%, T=0.5 -> call 4.76."""
        assert op.bsm_call(42, 40, 0.5, 0.10, 0.0, 0.20) == pytest.approx(4.76, abs=0.005)

    def test_matches_the_textbook_delta(self):
        """Hull Example 19.1: S=49, K=50, r=5%, sigma=20%, T=0.3846 -> delta 0.522."""
        assert op.bsm_call_delta(49, 50, 0.3846, 0.05, 0.0, 0.20) == pytest.approx(0.522, abs=0.001)

    def test_a_dividend_yield_lowers_the_call(self):
        assert op.bsm_call(100, 100, 2, 0.04, 0.03, 0.2) < op.bsm_call(100, 100, 2, 0.04, 0.0, 0.2)

    def test_expired_call_is_intrinsic(self):
        assert op.bsm_call(110, 100, 0, 0.04, 0.0, 0.2) == 10.0

    @pytest.mark.parametrize("vol", [0.12, 0.3, 0.8])
    def test_implied_vol_recovers_the_input(self, vol):
        price = op.bsm_call(60, 55, 1.9, 0.045, 0.03, vol)
        assert op.implied_vol(price, 60, 55, 1.9, 0.045, 0.03) == pytest.approx(vol, abs=1e-4)

    def test_put_call_parity(self):
        c = op.bsm_call(100, 95, 1.5, 0.04, 0.02, 0.3)
        p = op.bsm_put(100, 95, 1.5, 0.04, 0.02, 0.3)
        assert c - p == pytest.approx(100 * math.exp(-0.02 * 1.5) - 95 * math.exp(-0.04 * 1.5))

    def test_matches_the_textbook_put(self):
        """Hull Example 15.6 again: the put on S=42, K=40, r=10%, sigma=20%, T=0.5 is 0.81."""
        assert op.bsm_put(42, 40, 0.5, 0.10, 0.0, 0.20) == pytest.approx(0.81, abs=0.005)

    def test_put_delta_is_call_delta_less_one_without_dividends(self):
        cd = op.bsm_call_delta(49, 50, 0.3846, 0.05, 0.0, 0.2)
        assert op.bsm_put_delta(49, 50, 0.3846, 0.05, 0.0, 0.2) == pytest.approx(cd - 1)

    @pytest.mark.parametrize("vol", [0.15, 0.4])
    def test_put_implied_vol_recovers_the_input(self, vol):
        price = op.bsm_put(60, 65, 1.9, 0.045, 0.03, vol)
        assert op.implied_vol(price, 60, 65, 1.9, 0.045, 0.03, "put") == pytest.approx(vol, abs=1e-4)

    def test_implied_vol_is_undefined_below_intrinsic(self):
        """A quote under discounted intrinsic has no volatility; NaN, not a number."""
        assert math.isnan(op.implied_vol(3.0, 60, 50, 1.0, 0.04, 0.0))

    def test_the_vendor_iv_failure_case_prices_sanely(self):
        """KO 2024-03-01, 55 call to 2026-01-16 quoted 8.30/8.60 on a ~$59.9
        stock: the vendor said IV 1.5% and delta 1.00. Recomputed, it is an
        ordinary low-teens volatility and a delta well under one."""
        c = op.Contract("KO260116C00055000", pd.Timestamp("2026-01-16"), 55.0, "call",
                        8.30, 8.60, 784, 10)
        p = op.price_contract(c, 59.9, "2024-03-01", rate=0.0453, q=0.031)
        assert 0.08 < p.iv < 0.25
        assert 0.6 < p.delta < 0.95


# --- data ---------------------------------------------------------------------


def _row(strike, bid, ask, expiry="2026-01-16", kind="call", date="2024-03-01", oi=500):
    return {"contractID": f"X{expiry}{kind[0]}{strike}", "symbol": "X", "expiration": expiry,
            "strike": str(strike), "type": kind, "last": "0", "mark": "0",
            "bid": str(bid), "ask": str(ask), "bid_size": "1", "ask_size": "1",
            "volume": "1", "open_interest": str(oi), "date": date,
            "implied_volatility": "0.01488", "delta": "1.00000"}


class TestChain:
    def test_parses_quotes_and_ignores_vendor_greeks(self):
        payload = {"data": [_row(55, 8.3, 8.6)]}
        with patch.object(op, "_make_api_request", return_value=payload):
            (c,) = op.option_chain("X", "2024-03-01")
        assert (c.strike, c.bid, c.ask, c.kind) == (55.0, 8.3, 8.6, "call")
        assert c.mid == pytest.approx(8.45)
        assert c.spread == pytest.approx(0.3 / 8.45)
        assert not hasattr(c, "delta")

    def test_a_row_quoted_after_the_as_of_date_is_dropped(self):
        payload = {"data": [_row(55, 8.3, 8.6), _row(60, 5, 5.2, date="2024-03-04")]}
        with patch.object(op, "_make_api_request", return_value=payload):
            chain = op.option_chain("X", "2024-03-01")
        assert [c.strike for c in chain] == [55.0]

    def test_a_one_sided_quote_has_no_mid(self):
        c = op._parse_chain({"data": [_row(55, 0, 8.6)]}, pd.Timestamp("2024-03-01"))[0]
        assert not c.two_sided and math.isnan(c.mid)


class TestRatesAndDividends:
    def test_rate_is_the_last_print_on_or_before_the_date(self):
        payload = {"data": [{"date": "2024-03-04", "value": "4.60"},
                            {"date": "2024-03-01", "value": "4.53"},
                            {"date": "2024-02-29", "value": "4.64"}]}
        with patch.object(op, "_make_api_request", return_value=payload):
            assert op.risk_free_rate("2024-03-02") == pytest.approx(0.0453)

    def test_a_company_that_never_paid_has_a_zero_yield(self):
        with patch.object(op, "_make_api_request", return_value={"data": []}):
            assert op.dividend_yield("RKLB", "2025-09-02", 25.0) == 0.0

    def test_dividend_yield_counts_only_the_trailing_year_already_ex(self):
        payload = {"data": [
            {"ex_dividend_date": "2024-03-14", "amount": "0.485"},   # after the date
            {"ex_dividend_date": "2023-11-30", "amount": "0.46"},
            {"ex_dividend_date": "2023-09-14", "amount": "0.46"},
            {"ex_dividend_date": "2023-06-15", "amount": "0.46"},
            {"ex_dividend_date": "2023-03-16", "amount": "0.46"},
            {"ex_dividend_date": "2022-11-30", "amount": "0.44"},    # over a year back
        ]}
        with patch.object(op, "_make_api_request", return_value=payload):
            assert op.dividend_yield("X", "2024-03-01", 60.0) == pytest.approx(4 * 0.46 / 60)


# --- selection ------------------------------------------------------------------


def _chain(s=100.0, date="2025-09-02", rate=0.04, q=0.0, vol=0.35):
    """Calls priced off a known volatility across two expiries, plus noise contracts."""
    out = []
    for expiry in ("2026-06-18", "2027-01-15"):
        t = (pd.Timestamp(expiry) - pd.Timestamp(date)).days / 365
        for k in range(50, 160, 10):
            v = op.bsm_call(s, k, t, rate, q, vol)
            out.append(op.Contract(f"C{expiry}{k}", pd.Timestamp(expiry), float(k), "call",
                                   round(v * 0.99, 2), round(v * 1.01, 2), 500, 10))
    out.append(op.Contract("P", pd.Timestamp("2027-01-15"), 100.0, "put", 9, 10, 500, 10))
    out.append(op.Contract("short", pd.Timestamp("2025-12-19"), 100.0, "call", 7, 7.2, 500, 10))
    return out


class TestSelectCall:
    def test_picks_the_shortest_expiry_past_the_horizon_and_buffer(self):
        """126 + 63 trading days from 2025-09-02 is about 2026-06-10: the June
        2026 expiry qualifies, the December 2025 one does not."""
        p = op.select_call(_chain(), 100.0, "2025-09-02", 0.04, 0.0, 126, 0.75)
        assert p.contract.expiry == pd.Timestamp("2026-06-18")

    @pytest.mark.parametrize("target", [0.75, 0.5])
    def test_picks_the_strike_nearest_the_target_delta(self, target):
        p = op.select_call(_chain(), 100.0, "2025-09-02", 0.04, 0.0, 126, target)
        same_expiry = [
            op.price_contract(c, 100.0, "2025-09-02", 0.04, 0.0) for c in _chain()
            if c.kind == "call" and c.expiry == p.contract.expiry
        ]
        assert abs(p.delta - target) == min(abs(x.delta - target) for x in same_expiry)
        assert p.iv == pytest.approx(0.35, abs=0.02)

    def test_a_deeper_target_means_a_lower_strike(self):
        deep = op.select_call(_chain(), 100.0, "2025-09-02", 0.04, 0.0, 126, 0.75)
        atm = op.select_call(_chain(), 100.0, "2025-09-02", 0.04, 0.0, 126, 0.5)
        assert deep.contract.strike < atm.contract.strike

    def test_an_untraded_expiry_is_skipped_for_the_next_liquid_one(self):
        """KO at 2024-03-01: the shortest qualifying series had zero open interest."""
        chain = [c if c.expiry != pd.Timestamp("2026-06-18") else
                 op.Contract(c.contract_id, c.expiry, c.strike, c.kind, c.bid, c.ask, 0, 0)
                 for c in _chain()]
        p = op.select_call(chain, 100.0, "2025-09-02", 0.04, 0.0, 126, 0.75)
        assert p.contract.expiry == pd.Timestamp("2027-01-15")

    def test_with_nothing_liquid_the_shortest_is_returned_for_the_screen(self):
        chain = [op.Contract(c.contract_id, c.expiry, c.strike, c.kind, c.bid, c.ask, 0, 0)
                 for c in _chain()]
        p = op.select_call(chain, 100.0, "2025-09-02", 0.04, 0.0, 126, 0.75)
        assert p.contract.expiry == pd.Timestamp("2026-06-18")
        assert not op.is_liquid(p.contract)

    def test_nothing_long_enough_is_none(self):
        assert op.select_call(_chain(), 100.0, "2025-09-02", 0.04, 0.0, 504, 0.75) is None

    def test_breakeven_and_leverage(self):
        p = op.select_call(_chain(), 100.0, "2025-09-02", 0.04, 0.0, 126, 0.75)
        c = p.contract
        assert p.breakeven_move == pytest.approx((c.strike + c.ask) / 100 - 1)
        assert p.leverage == pytest.approx(p.delta * 100 / c.mid)
        assert p.extrinsic == pytest.approx(c.ask - max(100 - c.strike, 0))


class TestSelectPut:
    def _chain(self, s=100.0, date="2025-09-02", vol=0.35):
        out = []
        for expiry in ("2026-06-18", "2027-01-15"):
            t = (pd.Timestamp(expiry) - pd.Timestamp(date)).days / 365
            for k in range(60, 170, 10):
                v = op.bsm_put(s, k, t, 0.04, 0.0, vol)
                out.append(op.Contract(f"P{expiry}{k}", pd.Timestamp(expiry), float(k), "put",
                                       round(v * 0.99, 2), round(v * 1.01, 2), 500, 10))
        return out + _chain()  # calls alongside must be ignored

    @pytest.mark.parametrize("target", [0.75, 0.5])
    def test_picks_the_put_nearest_the_target_magnitude(self, target):
        p = op.select_put(self._chain(), 100.0, "2025-09-02", 0.04, 0.0, 126, target)
        assert p.contract.kind == "put" and p.delta < 0
        same = [op.price_contract(c, 100.0, "2025-09-02", 0.04, 0.0) for c in self._chain()
                if c.kind == "put" and c.expiry == p.contract.expiry]
        assert abs(abs(p.delta) - target) == min(abs(abs(x.delta) - target) for x in same)
        assert p.iv == pytest.approx(0.35, abs=0.02)

    def test_a_deeper_put_has_a_higher_strike(self):
        deep = op.select_put(self._chain(), 100.0, "2025-09-02", 0.04, 0.0, 126, 0.75)
        atm = op.select_put(self._chain(), 100.0, "2025-09-02", 0.04, 0.0, 126, 0.5)
        assert deep.contract.strike > atm.contract.strike

    def test_put_breakeven_is_the_fall_it_needs(self):
        p = op.select_put(self._chain(), 100.0, "2025-09-02", 0.04, 0.0, 126, 0.5)
        c = p.contract
        assert p.breakeven_move == pytest.approx((c.strike - c.ask) / 100 - 1)
        assert p.breakeven_move < 0
