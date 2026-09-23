"""The OHLCV cache: one file per symbol, fresh only on the day it was written.

A current-day request also refetches past a TTL, so a run started before the
day's bar was final is not served that snapshot all day (#1150). Keying the file
by symbol rather than by day keeps the cache from growing a file per symbol per
day (#1330).
"""
from __future__ import annotations

import os

import pandas as pd
import pytest

import tradingagents.dataflows.stockstats_utils as su

NOW = pd.Timestamp("2026-07-18 12:00")
STALE = su.OHLCV_CACHE_TTL_SECONDS + 60


def _write(tmp_path, name="AAPL-YFin-data.csv", age_seconds=0.0, last_date="2026-07-17"):
    f = tmp_path / name
    pd.DataFrame({"Date": [last_date], "Close": [100.0]}).to_csv(f, index=False)
# pandas reads a naive Timestamp's .timestamp() as UTC, but the code under
# test reads the file's mtime back with pd.Timestamp.fromtimestamp(), which
# returns LOCAL time. Seeding the mtime through pandas therefore lands a day
# early anywhere west of UTC, and _cache_is_fresh rejects the file. Going via
# datetime keeps both sides local, so these pass off a UTC machine too.
    written = NOW.to_pydatetime().timestamp() - age_seconds
    os.utime(f, (written, written))
    return f


def _load(tmp_path, monkeypatch, curr_date, download):
    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(su.pd.Timestamp, "today", staticmethod(lambda: NOW))
    monkeypatch.setattr(su.yf, "download", download)
    return su.load_ohlcv("AAPL", curr_date)


def _fail_download(*a, **k):
    raise AssertionError("fresh cache must not refetch")


@pytest.mark.unit
def test_current_day_cache_past_ttl_is_not_fresh(tmp_path):
    # Today's bar missing or still in progress: row inspection can't tell, so the TTL governs.
    assert su._cache_is_fresh(_write(tmp_path, age_seconds=STALE), NOW.normalize(), NOW) is False
    f = _write(tmp_path, age_seconds=STALE, last_date="2026-07-18")
    assert su._cache_is_fresh(f, NOW.normalize(), NOW) is False


@pytest.mark.unit
def test_recent_cache_is_fresh(tmp_path):
    # Written moments ago: don't hammer the vendor (weekend/holiday guard).
    assert su._cache_is_fresh(_write(tmp_path), NOW.normalize(), NOW) is True


@pytest.mark.unit
def test_historical_request_uses_todays_cache_past_the_ttl(tmp_path):
    f = _write(tmp_path, age_seconds=STALE, last_date="2026-04-30")
    assert su._cache_is_fresh(f, pd.Timestamp("2026-05-01"), NOW) is True


@pytest.mark.unit
def test_a_download_from_an_earlier_day_is_not_fresh(tmp_path):
    f = _write(tmp_path, age_seconds=13 * 3600)  # yesterday 23:00
    assert su._cache_is_fresh(f, pd.Timestamp("2026-05-01"), NOW) is False


@pytest.mark.unit
def test_load_ohlcv_refetches_stale_same_day_cache(tmp_path, monkeypatch):
    """End-to-end: the freshness check is wired into load_ohlcv's cache branch."""
    _write(tmp_path, age_seconds=STALE)
    calls = []

    def _fake_download(*a, **k):
        calls.append(1)
        return pd.DataFrame(
            {"Date": pd.to_datetime(["2026-07-17", "2026-07-18"]), "Close": [100.0, 222.0]}
        ).set_index("Date")

    out = _load(tmp_path, monkeypatch, "2026-07-18", _fake_download)
    assert calls, "stale same-day cache must trigger a refetch"
    assert 222.0 in out["Close"].values, "refreshed close must reach the caller"


@pytest.mark.unit
def test_load_ohlcv_reuses_fresh_same_day_cache(tmp_path, monkeypatch):
    _write(tmp_path, last_date="2026-07-18")
    _load(tmp_path, monkeypatch, "2026-07-18", _fail_download)


@pytest.mark.unit
def test_one_cache_file_per_symbol_across_days(tmp_path, monkeypatch):
    """A later day's download replaces the symbol's file instead of adding one (#1330)."""
    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    frame = pd.DataFrame({"Date": pd.to_datetime(["2026-07-16", "2026-07-17"]), "Close": [1.0, 2.0]})
    downloads = []
    monkeypatch.setattr(su.yf, "download", lambda *a, **k: downloads.append(1) or frame.set_index("Date"))

    for day in ("2026-07-18 10:00", "2026-07-19 10:00", "2026-07-20 10:00"):
        now = pd.Timestamp(day)
        monkeypatch.setattr(su.pd.Timestamp, "today", staticmethod(lambda now=now: now))
        su.load_ohlcv("AAPL", "2026-07-17")
        written = list(tmp_path.glob("AAPL-*.csv"))
        seeded = now.to_pydatetime().timestamp()  # local, see note above
        os.utime(written[0], (seeded, seeded))

    assert len(downloads) == 3, "each new day refetches"
    assert [p.name for p in tmp_path.iterdir()] == ["AAPL-YFin-data.csv"]


# --- the window reaches back far enough for the analysis date -----------------------------
#
# Live, a nightly screen-run failed every cell dated before September 2021: the
# download was 5y back from *today*, so a 2019 analysis date had no rows at all
# and a 2022 one had 110 -- no 200-day SMA.


def _capture_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(su.pd.Timestamp, "today", staticmethod(lambda: NOW))
    starts = []

    def _download(*a, start, end, **k):
        starts.append(start)
        dates = pd.bdate_range(start, end, inclusive="left")
        return pd.DataFrame({"Date": dates, "Close": 1.0}).set_index("Date")

    monkeypatch.setattr(su.yf, "download", _download)
    return starts


@pytest.mark.unit
def test_an_old_analysis_date_downloads_from_five_years_before_its_own_year(tmp_path, monkeypatch):
    starts = _capture_downloads(tmp_path, monkeypatch)
    out = su.load_ohlcv("PYPL", "2019-09-03")
    assert starts == ["2014-01-01"]
    assert out["Date"].min() <= pd.Timestamp("2017-09-03"), "two years of history before the date"
    assert out["Date"].max() <= pd.Timestamp("2019-09-03"), "still no look-ahead"


@pytest.mark.unit
def test_a_date_near_the_windows_edge_also_gets_the_longer_history(tmp_path, monkeypatch):
    """Within the 5y window but with too little of it before the date: META 2022-03-01."""
    starts = _capture_downloads(tmp_path, monkeypatch)
    su.load_ohlcv("META", "2022-03-01")
    assert starts == ["2017-01-01"]


@pytest.mark.unit
def test_a_recent_date_keeps_the_shared_five_year_file(tmp_path, monkeypatch):
    starts = _capture_downloads(tmp_path, monkeypatch)
    su.load_ohlcv("AAPL", "2024-03-01")
    assert starts == ["2021-07-18"]
    assert [p.name for p in tmp_path.iterdir()] == ["AAPL-YFin-data.csv"]


@pytest.mark.unit
def test_old_dates_in_one_year_share_a_file_and_leave_the_recent_one_alone(tmp_path, monkeypatch):
    starts = _capture_downloads(tmp_path, monkeypatch)
    seeded = NOW.to_pydatetime().timestamp()  # local, see note above
    for day in ("2019-03-01", "2019-09-03", "2024-03-01"):
        su.load_ohlcv("PYPL", day)
        for f in tmp_path.iterdir():
            os.utime(f, (seeded, seeded))
    assert starts == ["2014-01-01", "2021-07-18"], "the second 2019 date reuses the first's download"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["PYPL-YFin-data-from2014.csv", "PYPL-YFin-data.csv"]
