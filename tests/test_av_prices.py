"""The screener's Alpha Vantage price tier: deterministic answers, split-only
closes for market caps, and a same-day disk cache that is off unless asked for."""

from __future__ import annotations

import os
import time
from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.mandates.tools import financials as fin
from tradingagents.screener import prices

# Newest first, as Alpha Vantage sends it. A 2-for-1 split on 2024-06-10.
CSV = (
    "timestamp,open,high,low,close,adjusted_close,volume,dividend_amount,split_coefficient\n"
    "2024-06-11,51,52,50,51,51,2000,0,1.0\n"
    "2024-06-10,50,51,49,50,50,2000,0,2.0\n"
    "2024-06-07,100,102,98,100,49,1000,0.5,1.0\n"
    "2024-06-06,98,100,97,99,48.5,1000,0,1.0\n"
)


@pytest.fixture(autouse=True)
def _fresh():
    fin.alpha_vantage_daily_strict.cache_clear()
    yield
    fin.alpha_vantage_daily_strict.cache_clear()


class TestSplitClose:
    def test_closes_before_a_split_are_divided_by_it(self):
        with patch.object(fin, "_make_api_request", return_value=CSV):
            f = fin.alpha_vantage_daily_strict("X")
        assert list(f["Split Close"]) == [49.5, 50.0, 50.0, 51.0]
        assert list(f["Raw Close"]) == [99.0, 100.0, 50.0, 51.0]

    def test_split_close_is_not_dividend_adjusted(self):
        with patch.object(fin, "_make_api_request", return_value=CSV):
            f = fin.alpha_vantage_daily_strict("X")
        # Close carries the dividend adjustment; Split Close must not.
        assert f.loc["2024-06-07", "Close"] == 49.0
        assert f.loc["2024-06-07", "Split Close"] == 50.0

    def test_price_history_reads_alpha_vantage_first(self):
        with patch.object(fin, "_make_api_request", return_value=CSV), \
             patch.object(fin, "_price_history_cached") as yahoo:
            s = fin.price_history("X", pd.Timestamp("2024-06-01"), pd.Timestamp("2024-06-10"))
        yahoo.assert_not_called()
        assert list(s) == [49.5, 50.0, 50.0]

    def test_price_history_falls_back_to_yahoo(self):
        yahoo = pd.Series([7.0], index=[pd.Timestamp("2024-06-07")])
        with patch.object(fin, "_make_api_request", side_effect=RuntimeError("down")), \
             patch.object(fin, "_price_history_cached", return_value=yahoo):
            s = fin.price_history("X", pd.Timestamp("2024-06-01"), pd.Timestamp("2024-06-10"))
        assert list(s) == [7.0]


class TestDownloadAv:
    def test_frames_are_sliced_to_the_window_with_yahoo_columns(self):
        with patch.object(fin, "_make_api_request", return_value=CSV):
            data = prices.download_av(["X"], "2024-06-07", "2024-06-11")
        f = data.frames["X"]
        assert list(f.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert [d.strftime("%m-%d") for d in f.index] == ["06-07", "06-10"]

    def test_a_vendor_error_is_unavailable_and_no_history_is_not(self):
        def answer(function, params):
            if params["symbol"] == "BAD":
                raise RuntimeError("Invalid API call")
            if params["symbol"] == "DEAD":
                return '{"Error Message": "no such symbol"}'
            return CSV

        with patch.object(fin, "_make_api_request", side_effect=answer):
            data = prices.download_av(["GOOD", "BAD", "DEAD"], "2024-06-01", "2024-06-12", attempts=1)
        assert set(data.frames) == {"GOOD"}
        assert data.unavailable == {"BAD"}
        assert "DEAD" not in data.unavailable  # an answer, not an outage

    def test_the_same_answer_twice_is_the_same_frame(self):
        """The point of the switch: two runs of one screen must agree."""
        with patch.object(fin, "_make_api_request", return_value=CSV):
            a = prices.download_av(["X"], "2024-06-01", "2024-06-12").frames["X"]
            fin.alpha_vantage_daily_strict.cache_clear()
            b = prices.download_av(["X"], "2024-06-01", "2024-06-12").frames["X"]
        pd.testing.assert_frame_equal(a, b)


class TestDiskCache:
    def test_off_by_default(self, tmp_path):
        assert fin._daily_cache_dir is None
        with patch.object(fin, "_make_api_request", return_value=CSV):
            fin.alpha_vantage_daily_strict("X")
        assert not list(tmp_path.rglob("*.gz"))

    def test_a_cached_day_needs_no_request_and_skips_the_limiter(self, tmp_path):
        with fin.daily_disk_cache(tmp_path):
            with patch.object(fin, "_make_api_request", return_value=CSV):
                fin.alpha_vantage_daily_strict("X")
            fin.alpha_vantage_daily_strict.cache_clear()
            assert fin.daily_is_cached("X")

            class Limiter:
                calls = 0

                def acquire(self, n=1):
                    Limiter.calls += 1

            with patch.object(fin, "_make_api_request", side_effect=AssertionError("fetched")):
                data = prices.download_av(["X"], "2024-06-01", "2024-06-12", limiter=Limiter())
        assert "X" in data.frames
        assert Limiter.calls == 0

    def test_yesterdays_copy_is_refetched(self, tmp_path):
        with fin.daily_disk_cache(tmp_path):
            with patch.object(fin, "_make_api_request", return_value=CSV):
                fin.alpha_vantage_daily_strict("X")
            path = tmp_path / "av_daily" / "X.csv.gz"
            day_ago = time.time() - 86400
            os.utime(path, (day_ago, day_ago))
            assert not fin.daily_is_cached("X")

    def test_an_error_body_is_never_written(self, tmp_path):
        with fin.daily_disk_cache(tmp_path), \
                patch.object(fin, "_make_api_request", return_value='{"Error Message": "x"}'):
            assert fin.alpha_vantage_daily_strict("X").empty
        assert not (tmp_path / "av_daily" / "X.csv.gz").exists()

    def test_the_block_restores_the_previous_setting(self, tmp_path):
        with fin.daily_disk_cache(tmp_path):
            assert fin._daily_cache_dir == tmp_path / "av_daily"
        assert fin._daily_cache_dir is None

    def test_old_copies_are_purged(self, tmp_path):
        d = tmp_path / "av_daily"
        d.mkdir()
        old = d / "OLD.csv.gz"
        old.write_bytes(b"x")
        week_ago = time.time() - 7 * 86400
        os.utime(old, (week_ago, week_ago))
        with fin.daily_disk_cache(tmp_path):
            pass
        assert not old.exists()
