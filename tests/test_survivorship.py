"""Survivorship: a historical question must be able to see companies that later died.

Four places could drop a delisted name and so turn a historical study into a
study of survivors: the universe (today's listings), prices (Yahoo drops a
delisted ticker's history), fundamentals (Alpha Vantage drops its statements)
and grading (no prices, so the decision never settles). The first, second and
fourth are fixed; the third cannot be, so it is counted and reported.
"""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.mandates.tools import financials as fin
from tradingagents.screener import prices, screen, universe

PAST = """symbol,name,exchange,assetType,ipoDate,delistingDate,status
AAA,Alpha Inc,NYSE,Stock,2010-01-04,null,Active
DDD,Dead Corp,NYSE,Stock,2011-01-04,null,Active
"""
NOW = """symbol,name,exchange,assetType,ipoDate,delistingDate,status
AAA,Alpha Inc,NYSE,Stock,2010-01-04,null,Active
NEW,Newco,NYSE,Stock,2024-01-04,null,Active
"""


def _listing(calls):
    def fake(function, params):
        calls.append(dict(params))
        return PAST if params.get("date") else NOW
    return fake


# --- tier 0: the universe as it stood --------------------------------------------


class TestUniverse:

    @pytest.fixture(autouse=True)
    def _today(self, monkeypatch):
        monkeypatch.setattr(universe, "get_current_date", lambda: "2026-09-18")

    def test_a_past_date_uses_the_listings_active_on_that_date(self):
        calls = []
        with patch.object(universe, "_make_api_request", side_effect=_listing(calls)):
            names = {c.symbol: c for c in universe.load_universe("2022-03-01")}
        assert {"date": "2022-03-01", "state": "active"} in calls
        assert set(names) == {"AAA", "DDD"}, "today's listings would drop DDD and add NEW"

    def test_names_gone_since_are_marked(self):
        with patch.object(universe, "_make_api_request", side_effect=_listing([])):
            names = {c.symbol: c for c in universe.load_universe("2022-03-01")}
        assert names["DDD"].delisted_since is True
        assert names["AAA"].delisted_since is False

    def test_a_live_screen_makes_one_call_and_marks_nothing(self):
        calls = []
        with patch.object(universe, "_make_api_request", side_effect=_listing(calls)):
            names = universe.load_universe("2026-09-18")
        assert calls == [{}]
        assert not any(c.delisted_since for c in names)

    def test_dates_before_the_vendors_history_are_refused(self):
        with pytest.raises(ValueError, match="historical listings start at 2010-01-01"):
            universe.load_universe("2009-12-31")


# --- tier 1: prices for the names Yahoo forgot ---------------------------------------


def _yahoo(frames):
    """A yf.download stand-in answering for the symbols in ``frames`` only."""
    def download(batch, **kw):
        cols = {}
        for sym in batch:
            if sym in frames:
                for c, series in frames[sym].items():
                    cols[(sym, c)] = series
        return pd.DataFrame(cols)
    return download


def _bars(start="2021-01-04", n=300, price=50.0):
    idx = pd.bdate_range(start, periods=n)
    close = pd.Series([price + i * 0.01 for i in range(n)], index=idx)
    return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close,
                         "Volume": pd.Series(1e6, index=idx)})


class TestPriceFallback:

    @pytest.fixture(autouse=True)
    def _no_wait(self):
        with patch("time.sleep"):
            yield

    def test_a_delisted_name_is_priced_from_alpha_vantage(self):
        with patch("yfinance.download", side_effect=_yahoo({"AAA": _bars()})), \
             patch.object(fin, "alpha_vantage_daily_strict", return_value=_bars()) as av:
            data = prices.download(["AAA", "DDD"], "2021-01-01", "2022-03-02", fallback={"DDD"})
        assert set(data.frames) == {"AAA", "DDD"}
        av.assert_called_once_with("DDD")

    def test_the_fallback_is_sliced_to_the_window(self):
        with patch("yfinance.download", side_effect=_yahoo({"AAA": _bars()})), \
             patch.object(fin, "alpha_vantage_daily_strict", return_value=_bars("2019-01-01", 1200)):
            data = prices.download(["AAA", "DDD"], "2021-01-01", "2022-03-02", fallback={"DDD"})
        assert data.frames["DDD"].index.min() >= pd.Timestamp("2021-01-01")
        assert data.frames["DDD"].index.max() < pd.Timestamp("2022-03-02")

    def test_names_not_in_the_fallback_cost_nothing(self):
        """A live screen's genuinely dead names must not each spend an API call."""
        with patch("yfinance.download", side_effect=_yahoo({"AAA": _bars()})), \
             patch.object(fin, "alpha_vantage_daily_strict") as av:
            data = prices.download(["AAA", "ZZZ"], "2021-01-01", "2022-03-02")
        av.assert_not_called()
        assert "ZZZ" not in data.frames

    def test_a_failing_fallback_is_reported_as_the_vendor_not_the_company(self):
        from tradingagents.dataflows.alpha_vantage_common import AlphaVantageRateLimitError

        with patch("yfinance.download", side_effect=_yahoo({"AAA": _bars()})), \
             patch.object(fin, "alpha_vantage_daily_strict",
                          side_effect=AlphaVantageRateLimitError("slow down")), \
             patch("time.sleep"):
            data = prices.download(["AAA", "DDD"], "2021-01-01", "2022-03-02", fallback={"DDD"})
        assert "DDD" in data.unavailable, "an outage must not read as 'no price history'"

    def test_a_vendor_outage_is_not_papered_over_by_the_fallback(self):
        """An all-empty batch is an outage: its names stay unavailable, not refetched."""
        with patch("yfinance.download", side_effect=_yahoo({})), \
             patch.object(fin, "alpha_vantage_daily_strict") as av, \
             patch("time.sleep"):
            data = prices.download(["AAA", "DDD"], "2021-01-01", "2022-03-02", fallback={"DDD"})
        av.assert_not_called()
        assert data.unavailable == {"AAA", "DDD"}


class TestAlphaVantageFrame:

    CSV = ("timestamp,open,high,low,close,adjusted_close,volume,dividend_amount,split_coefficient\n"
           "2022-03-02,20,22,19,21,10.5,1000,0,1\n"
           "2022-03-01,10,11,9,10,5,2000,0,1\n")

    def test_shaped_like_yahoo_and_adjusted(self):
        fin.alpha_vantage_daily_strict.cache_clear()
        with patch.object(fin, "_make_api_request", return_value=self.CSV):
            frame = fin.alpha_vantage_daily("DDD")
        assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert frame.index.is_monotonic_increasing
        # Adjusted by adjusted_close / close = 0.5 on both days.
        assert frame["Close"].tolist() == [5.0, 10.5]
        assert frame["Open"].tolist() == [5.0, 10.0]

    def test_an_error_notice_is_no_data(self):
        fin.alpha_vantage_daily_strict.cache_clear()
        with patch.object(fin, "_make_api_request", return_value='{"Error Message": "Invalid"}'):
            assert fin.alpha_vantage_daily("NOPE").empty

    def test_a_failure_is_not_cached(self):
        """One throttled call must not become 'no data' for the rest of the run."""
        fin.alpha_vantage_daily_strict.cache_clear()
        with patch.object(fin, "_make_api_request", side_effect=RuntimeError("throttled")):
            assert fin.alpha_vantage_daily("DDD").empty
        with patch.object(fin, "_make_api_request", return_value=self.CSV):
            assert len(fin.alpha_vantage_daily("DDD")) == 2


# --- tier 2: the loss that cannot be fixed is counted ----------------------------------


class TestSurvivorshipNote:

    def test_the_note_counts_each_tier(self):
        note = screen.survivorship_note(
            delisted={"D1", "D2", "D3", "D4"}, price_survivors=["D1", "D2", "D3", "OK"],
            examined=["D1", "D2", "OK"], lost=["D1", "D2"], eligible=["OK"],
        )
        assert "4 names in this universe have delisted since" in note
        assert "3 passed the price tier, 2 reached the fundamentals tier" in note
        assert "2 of those were excluded there only because no statements survive" in note
        assert "What survivorship bias remains is that count" in note

    def test_a_delisted_name_without_statements_says_why(self):
        assert "EDGAR had no single US-GAAP filer" in screen.DELISTED_NO_STATEMENTS


# --- grading: decisions on dead companies settle ------------------------------------------


def _series(start, n, first=100.0, step=1.0):
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"Close": [first + i * step for i in range(n)]}, index=idx)


def _grade(stock, bench, horizons=(5,), av=None):
    with patch("yfinance.Ticker") as cls, \
         patch.object(fin, "alpha_vantage_daily", return_value=av if av is not None else pd.DataFrame()):
        cls.side_effect = lambda sym: MagicMock(**{"history.return_value": bench if sym == "SPY" else stock})
        return TradingAgentsGraph._returns_at_horizons("DDD", "2022-03-01", horizons, "SPY")


class TestGradingDelistedNames:

    def test_a_name_yahoo_forgot_is_graded_from_alpha_vantage(self):
        settled = _grade(pd.DataFrame(), _series("2022-03-01", 10, 400, 0),
                         av=_series("2021-01-04", 400))
        assert 5 in settled

    def test_delisted_inside_the_horizon_settles_at_its_last_trade(self):
        # Trades 20 sessions then stops; the benchmark runs through a 60-day horizon.
        stock = _series("2022-03-01", 20, first=100.0, step=1.0)       # ends at 119
        bench = _series("2022-03-01", 80, first=400.0, step=0.0)       # flat benchmark
        raw, alpha, resolved = _grade(stock, bench, horizons=(60,))[60]
        assert raw == pytest.approx(0.19)
        assert alpha == pytest.approx(0.19)
        assert resolved == stock.index[-1].strftime("%Y-%m-%d")

    def test_it_waits_until_the_benchmark_shows_the_horizon_has_passed(self):
        stock = _series("2022-03-01", 20)
        bench = _series("2022-03-01", 40, 400, 0)   # delisted, but day 60 hasn't come yet
        assert _grade(stock, bench, horizons=(60,)) == {}

    def test_a_short_gap_is_a_late_print_not_a_delisting(self):
        """#1169 still holds: a stock merely behind the benchmark stays pending."""
        stock = _series("2022-03-01", 70)
        bench = _series("2022-03-01", 75, 400, 0)   # 5 sessions ahead, well under 14 days
        assert _grade(stock, bench, horizons=(72,)) == {}

    def test_an_ordinary_name_is_graded_exactly_as_before(self):
        stock = _series("2022-03-01", 10, first=100.0, step=2.0)
        bench = _series("2022-03-01", 10, first=400.0, step=0.0)
        raw, alpha, resolved = _grade(stock, bench, horizons=(5,))[5]
        assert raw == pytest.approx(0.10)
        assert resolved == stock.index[5].strftime("%Y-%m-%d")

    def test_no_data_anywhere_settles_nothing(self):
        assert _grade(pd.DataFrame(), _series("2022-03-01", 10, 400, 0)) == {}


# --- throttling inside a batch -------------------------------------------------------


class TestSecondLook:
    """Yahoo drops symbols from inside a batch when throttling; that is not a fact
    about those companies. Live, it once excluded AEM and AGNC as 'no price history'."""

    @pytest.fixture(autouse=True)
    def _no_wait(self):
        with patch("time.sleep"):
            yield

    def test_a_throttled_symbol_is_recovered(self):
        answers = iter([_yahoo({"AAA": _bars()}), _yahoo({"AEM": _bars()})])
        with patch("yfinance.download", side_effect=lambda batch, **kw: next(answers)(batch, **kw)) as dl:
            data = prices.download(["AAA", "AEM"], "2021-01-01", "2022-03-02")
        assert set(data.frames) == {"AAA", "AEM"}
        assert dl.call_args_list[1].args[0] == ["AEM"], "only the missing symbols are re-asked"

    def test_a_dead_symbol_is_believed_after_one_empty_look(self):
        with patch("yfinance.download", side_effect=_yahoo({"AAA": _bars()})) as dl:
            data = prices.download(["AAA", "DEAD"], "2021-01-01", "2022-03-02", attempts=3)
        assert set(data.frames) == {"AAA"}
        assert data.unavailable == set(), "a symbol Yahoo answers empty for twice has no history"
        assert dl.call_count == 2, "a look that recovers nothing ends the looking"

    def test_one_attempt_believes_the_first_answer(self):
        with patch("yfinance.download", side_effect=_yahoo({"AAA": _bars()})) as dl:
            prices.download(["AAA", "DEAD"], "2021-01-01", "2022-03-02", attempts=1)
        assert dl.call_count == 1

    def test_a_flat_frame_is_not_attributed_to_every_symbol(self):
        """A single un-keyed frame for a multi-symbol ask cannot say whose it is."""
        with patch("yfinance.download", return_value=_bars()):
            data = prices.download(["AAA", "BBB"], "2021-01-01", "2022-03-02", attempts=1)
        assert data.frames == {}


# --- universe hygiene: undashed warrants, rights, units, notes, preferreds ------------


class TestDerivativeLines:
    """About one Alpha Vantage 'Stock' row in seven is not a common share."""

    LISTED = {"ZION", "AGNC", "AEP", "GOOG", "MAR", "FOX", "ABLL", "AMZ"}

    @pytest.mark.parametrize("symbol, name", [
        ("ZIONO", "Zions Bancorporation N.A"),            # preferred, plain issuer name
        ("AGNCN", "AGNC Investment Corp"),                 # preferred, plain issuer name
        ("AEPPZ", "American Electric Power Company Inc"),  # P + letter: notes
        ("ABLLW", "Abacus Global Management Inc - Warrants (30/06/2028)"),
        ("VLDRW", "Velodyne Lidar Inc Warrant"),           # base not listed: the name decides
        ("BRRWU", "Columbus Circle Capital Corp I Units"),
        ("DYNC", "Dynegy Inc 700 Tangible Equity Units"),
        ("PRHIZ", "Presurance Holdings Inc Sr Nt"),
    ])
    def test_non_common_lines_are_recognised(self, symbol, name):
        assert universe.derivative_line(symbol, name, self.LISTED)

    @pytest.mark.parametrize("symbol, name", [
        ("GOOGL", "Alphabet Inc - Class A"),     # class letter, not a derivative code
        ("FOXA", "Fox Corporation - Class A"),
        ("MARPS", "Marine Petroleum Trust"),     # a trust's common units, not MAR + PS
        ("PFBC", "Preferred Bank"),              # the word alone is not an instrument
        ("UNTC", "Unit Corporation"),
        ("AMZN", "Amazon.com Inc"),              # four letters: no suffix convention
    ])
    def test_common_shares_are_left_alone(self, symbol, name):
        assert universe.derivative_line(symbol, name, self.LISTED) is None

    def test_the_universe_drops_them(self, monkeypatch):
        monkeypatch.setattr(universe, "get_current_date", lambda: "2026-09-18")
        listing = ("symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"
                   "ZION,Zions Bancorporation N.A,NASDAQ,Stock,2000-01-03,null,Active\n"
                   "ZIONO,Zions Bancorporation N.A,NASDAQ,Stock,2014-06-10,null,Active\n"
                   "GOOG,Alphabet Inc - Class C,NASDAQ,Stock,2014-03-27,null,Active\n"
                   "GOOGL,Alphabet Inc - Class A,NASDAQ,Stock,2004-08-19,null,Active\n")
        with patch.object(universe, "_make_api_request", return_value=listing):
            assert [c.symbol for c in universe.load_universe("2026-09-18")] == ["GOOG", "GOOGL", "ZION"]
