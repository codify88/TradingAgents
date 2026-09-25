"""The exit-rule study on paths whose right answer is known."""
from __future__ import annotations

import pandas as pd
import pytest

from tradingagents.evaluation import exits


def _series(values, start="2020-01-01"):
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), dtype=float)


def _replay(values, holding=None):
    close = _series([100.0] * 80 + values)  # 80 days of history before the entry
    date = close.index[80].date().isoformat()
    return exits.replay("X", date, holding or len(values) - 1, "equity_value", lambda t: close)


@pytest.mark.unit
def test_a_trailing_stop_exits_after_the_peak_and_then_sits_in_cash():
    r = _replay([100, 120, 150, 120, 110, 200])  # peak 150, 20% below it is 120
    assert r.returns["hold"] == pytest.approx(1.0)
    assert r.returns["trailing 15%"] == pytest.approx(0.20)   # out at 120 on the fall from 150
    assert r.returns["trailing 25%"] == pytest.approx(0.10)   # 112.5 line: out at 110


@pytest.mark.unit
def test_tiered_sells_a_third_at_each_level():
    r = _replay([100, 125, 150, 100])
    # a third at 1.25, a third at 1.50, a third back at 1.00
    assert r.returns["tiered"] == pytest.approx((1.25 + 1.50 + 1.00) / 3 - 1)


@pytest.mark.unit
def test_the_rollercoaster_is_a_big_gain_mostly_given_back():
    assert _replay([100, 140, 110]).rollercoaster             # +40% peak, ends +10%
    assert not _replay([100, 140, 130]).rollercoaster          # gave back a quarter
    assert not _replay([100, 120, 100]).rollercoaster          # never reached +30%


@pytest.mark.unit
def test_an_unfinished_horizon_is_not_replayed():
    close = _series([100.0] * 100)
    assert exits.replay("X", close.index[90].date().isoformat(), 50, "v", lambda t: close) is None


@pytest.mark.unit
def test_only_settled_bullish_calls_are_studied_once_each():
    base = {"date": "2020-01-01", "holding": "756d", "mandate": "equity_value", "superseded": None}
    entries = [
        {**base, "ticker": "A", "rating": "Buy", "pending": False},
        {**base, "ticker": "A", "rating": "Buy", "pending": False},        # the same cell twice
        {**base, "ticker": "B", "rating": "Hold", "pending": False},
        {**base, "ticker": "C", "rating": "Overweight", "pending": True},
        {**base, "ticker": "D", "rating": "Overweight", "pending": False},
    ]
    assert [e["ticker"] for e in exits.settled_bullish(entries)] == ["A", "D"]


@pytest.mark.unit
def test_a_small_sample_says_so():
    r = _replay([100, 110, 120])
    text = exits.render([r])
    assert "Too few settled calls (1" in text and "not a recommendation" in text
