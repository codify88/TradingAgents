"""LEAPS build step 4: grading the instrument choice from option chains."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.mandates.tools import leaps_grading as lg, options as op

DAYS = pd.bdate_range("2025-01-02", periods=300)
ENTRY = DAYS[10]
EXPIRY = pd.Timestamp("2026-06-18")


def _frame(entry_price=100.0, exit_price=120.0, dividend_drag=0.0):
    """Raw closes step from entry to exit price at the horizon; adjusted closes
    add ``dividend_drag`` of total return the raw series does not show."""
    raw = pd.Series(entry_price, index=DAYS)
    raw.iloc[10 + 126:] = exit_price
    adj = raw * (1 + pd.Series([0.0] * 136 + [dividend_drag] * 164, index=DAYS))
    return pd.DataFrame({"Close": adj, "Raw Close": raw})


def _chain_on(entry_quotes, exit_quotes, kind="call"):
    """option_chain stand-in: entry-day and exit-day quotes for one contract set."""
    def chain(symbol, date):
        quotes = entry_quotes if pd.Timestamp(date) == ENTRY else exit_quotes.get(pd.Timestamp(date), {})
        return tuple(
            op.Contract(f"{kind[0].upper()}{k:g}", EXPIRY, k, kind, bid, ask, 1000, 10)
            for k, (bid, ask) in quotes.items()
        )
    return chain


def _grade(frame, entry_quotes, exit_quotes, target=0.75, kind="call"):
    with patch("tradingagents.mandates.tools.financials.alpha_vantage_daily_strict", return_value=frame), \
         patch.object(op, "option_chain", side_effect=_chain_on(entry_quotes, exit_quotes, kind)), \
         patch.object(op, "risk_free_rate", return_value=0.04), \
         patch.object(op, "dividend_yield", return_value=0.0):
        return lg.grade("X", ENTRY.strftime("%Y-%m-%d"), 126, target, kind)


def _entry_quotes():
    t = (EXPIRY - ENTRY).days / 365
    out = {}
    for k in (80.0, 90.0, 100.0, 110.0):
        v = op.bsm_call(100, k, t, 0.04, 0.0, 0.3)
        out[k] = (round(v * 0.99, 2), round(v * 1.01, 2))
    return out


class TestGrade:
    def test_bought_at_the_ask_sold_at_the_bid_on_the_exit_day(self):
        entry = _entry_quotes()
        exit_day = DAYS[10 + 126]
        exit_quotes = {exit_day: {k: (b + 15, a + 15) for k, (b, a) in entry.items()}}
        o = _grade(_frame(), entry, exit_quotes)
        assert o.exit_date == exit_day
        assert o.entry_ask == entry[float(o.contract_id[1:])][1]
        assert o.exit_value == pytest.approx(entry[float(o.contract_id[1:])][0] + 15)
        assert o.stock_return == pytest.approx(0.20)

    def test_a_missing_exit_quote_looks_back_a_few_days(self):
        entry = _entry_quotes()
        day_before = DAYS[10 + 125]
        o = _grade(_frame(), entry, {day_before: {k: (b + 5, a + 5) for k, (b, a) in entry.items()}})
        assert o is not None and o.exit_date == day_before

    def test_no_exit_quote_at_all_is_ungradable(self):
        assert _grade(_frame(), _entry_quotes(), {}) is None

    def test_a_cell_not_yet_at_the_horizon_is_ungradable(self):
        short = _frame().iloc[:100]
        assert _grade(short, _entry_quotes(), {}) is None

    def test_edge_against_matched_shares(self):
        """A call that exactly tracks delta shares, net of its cost, has zero edge."""
        o = lg.OptionOutcome("C", "call", 0.75, delta=0.8, entry_ask=10.0, exit_value=26.0,
                             exit_date=DAYS[136], underlying_entry=100.0, stock_return=0.20)
        # Call P&L 16; eight-tenths of a share gained 0.8 * 100 * 0.2 = 16.
        assert o.edge_vs_matched == pytest.approx(0.0)
        assert o.option_return == pytest.approx(1.6)

    def test_forgone_dividends_count_against_the_call(self):
        o_plain = lg.OptionOutcome("C", "call", 0.75, 0.8, 10.0, 26.0, DAYS[136], 100.0, 0.20)
        o_div = lg.OptionOutcome("C", "call", 0.75, 0.8, 10.0, 26.0, DAYS[136], 100.0, 0.23)
        assert o_div.edge_vs_matched < o_plain.edge_vs_matched


class TestReview:
    def _entries(self):
        return [
            {"ticker": "A", "date": "2025-01-16", "rating": "Overweight",
             "decision": "**Rating**: Overweight\n\n**Instrument**: Call"},
            {"ticker": "B", "date": "2025-01-16", "rating": "Buy",
             "decision": "**Rating**: Buy\n\n**Instrument**: Stock"},
            {"ticker": "C", "date": "2025-01-16", "rating": "Hold", "decision": "**Rating**: Hold"},
        ]

    def test_only_positions_are_graded_and_the_instrument_is_read(self):
        with patch.object(lg, "grade", return_value=None):
            cells = lg.review(self._entries())
        assert [(c.ticker, c.instrument) for c in cells] == [("A", "Call"), ("B", "Stock")]

    def test_render_compares_chosen_calls_with_calls_passed_on(self):
        good = lg.OptionOutcome("C", "call", 0.75, 0.8, 10.0, 30.0, DAYS[136], 100.0, 0.20)
        bad = lg.OptionOutcome("C", "call", 0.75, 0.8, 10.0, 12.0, DAYS[136], 100.0, 0.20)
        with patch.object(lg, "grade", side_effect=[good, good, bad, bad]):
            text = lg.render(lg.review(self._entries()))
        assert "Calls the agents chose: 1, mean edge over matched shares +4.0%" in text
        assert "Calls they passed on (held as stock): 1, mean edge the call would have had -14.0%" in text

    def test_a_vendor_failure_is_reported_not_fatal(self):
        with patch.object(lg, "grade", side_effect=RuntimeError("rate limited")):
            cells = lg.review(self._entries())
        assert all(c.error for c in cells)
        text = lg.render(cells)
        assert "vendor error" in text and "should be rerun" in text

    def test_nothing_to_grade_says_why(self):
        assert "a Hold takes no position" in lg.render([])


class TestPuts:
    def _entry_quotes(self):
        t = (EXPIRY - ENTRY).days / 365
        out = {}
        for k in (90.0, 100.0, 110.0, 120.0):
            v = op.bsm_put(100, k, t, 0.04, 0.0, 0.3)
            out[k] = (round(v * 0.99, 2), round(v * 1.01, 2))
        return out

    def test_a_put_on_a_falling_stock_gains_and_is_matched_by_a_short(self):
        entry = self._entry_quotes()
        exit_day = DAYS[10 + 126]
        exit_quotes = {exit_day: {k: (b + 12, a + 12) for k, (b, a) in entry.items()}}
        o = _grade(_frame(exit_price=85.0), entry, exit_quotes, kind="put")
        assert o.kind == "put" and o.delta < 0
        assert o.stock_return == pytest.approx(-0.15)
        assert o.option_return > 0
        # A short of |delta| shares also gained; the edge nets it off.
        matched = o.delta * 100 * -0.15
        assert o.edge_vs_matched == pytest.approx((o.exit_value - o.entry_ask - matched) / 100)

    def test_a_put_held_to_expiry_is_worth_strike_minus_stock(self):
        c = op.Contract("P110", pd.Timestamp("2025-03-21"), 110.0, "put", 12, 12.5, 1000, 10)
        frame = _frame(exit_price=95.0)
        raw = frame["Raw Close"].copy()
        raw.loc[:] = 95.0
        value, when = lg._exit_value(c, DAYS, raw, DAYS[200], "X")
        assert value == pytest.approx(15.0) and when == c.expiry

    def test_bearish_cells_are_graded_on_puts_in_their_own_section(self):
        entries = [
            {"ticker": "A", "date": "2025-01-16", "rating": "Underweight", "decision": "**Rating**: Underweight"},
            {"ticker": "B", "date": "2025-01-16", "rating": "Sell", "decision": "**Rating**: Sell"},
        ]
        won = lg.OptionOutcome("P", "put", 0.75, -0.7, 10.0, 20.0, DAYS[136], 100.0, -0.10)
        lost = lg.OptionOutcome("P", "put", 0.75, -0.7, 10.0, 4.0, DAYS[136], 100.0, 0.10)
        kinds = []

        def fake(symbol, date, horizon, target, kind):
            kinds.append(kind)
            return won if symbol == "A" else lost

        with patch.object(lg, "grade", side_effect=fake):
            text = lg.render(lg.review(entries))
        assert set(kinds) == {"put"}
        assert "### Puts on Underweight and Sell" in text and "### Calls" not in text
        assert "The stock fell in 1 of 2" in text
        # A: 10 - (-0.7*100*-0.10)=10-7=+3%; B: -6 - (-0.7*100*0.10)=-6+7=+1%.
        assert "mean edge over a matched short +2.0%" in text
        assert "the put beat its matched short in 2 of 2" in text
