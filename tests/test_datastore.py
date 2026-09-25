"""The point-in-time store: final responses fetched once, everything else by date."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

import tradingagents.datastore as ds
from tradingagents.datastore import policy as pol
from tradingagents.datastore.store import DataStore, params_key
from tradingagents.datastore.throttle import acquire

NY = pol.NEW_YORK


@pytest.fixture
def store_on(tmp_path, monkeypatch):
    """The store in read_write mode against a temporary file."""
    monkeypatch.setenv("TRADINGAGENTS_DATA_STORE", "read_write")
    monkeypatch.setattr(ds, "_config", lambda: {
        "data_cache_dir": str(tmp_path), "data_store_path": None,
        "av_requests_per_minute": 0,  # no throttle waits in unit tests
    })
    ds._stores.clear()
    yield ds.get_store()
    ds._stores.clear()


def _fetcher(body="payload"):
    calls = []

    def fetch():
        calls.append(1)
        return body

    return fetch, calls


# --- the freshness rule -------------------------------------------------------------

NOW = datetime(2026, 9, 25, 20, 0, tzinfo=NY)  # a Friday evening, market closed


@pytest.mark.unit
@pytest.mark.parametrize("endpoint,params,final", [
    ("HISTORICAL_OPTIONS", {"symbol": "KO", "date": "2019-06-03"}, True),
    ("HISTORICAL_OPTIONS", {"symbol": "KO", "date": "2026-09-25"}, False),
    ("HISTORICAL_OPTIONS", {"symbol": "KO"}, False),
    ("LISTING_STATUS", {"date": "2020-01-02"}, True),
    ("NEWS_SENTIMENT", {"tickers": "AAPL", "time_to": "20260901T2359"}, True),
    ("NEWS_SENTIMENT", {"tickers": "AAPL", "time_to": "20260922T2359"}, False),
    ("EARNINGS_CALL_TRANSCRIPT", {"symbol": "IBM", "quarter": "2019Q2"}, True),
    ("EARNINGS_CALL_TRANSCRIPT", {"symbol": "IBM", "quarter": "2026Q2"}, False),
    ("TIME_SERIES_DAILY_ADJUSTED", {"symbol": "IBM", "outputsize": "full"}, False),
    ("INCOME_STATEMENT", {"symbol": "IBM"}, False),
])
def test_only_closed_past_periods_are_final(endpoint, params, final):
    assert pol.policy_for(endpoint, params, NOW).final is final


@pytest.mark.unit
def test_holdings_and_etf_profiles_are_snapshotted():
    assert pol.policy_for("INSTITUTIONAL_HOLDINGS", {"symbol": "IBM"}, NOW).snapshot
    assert pol.policy_for("ETF_PROFILE", {"symbol": "SPY"}, NOW).snapshot
    assert not pol.policy_for("OVERVIEW", {"symbol": "IBM"}, NOW).snapshot


@pytest.mark.unit
def test_a_daily_response_serves_only_its_new_york_date():
    p = pol.policy_for("OVERVIEW", {"symbol": "IBM"}, NOW)
    this_morning = datetime(2026, 9, 25, 7, 0, tzinfo=NY)
    yesterday_evening = datetime(2026, 9, 24, 21, 0, tzinfo=NY)
    assert pol.is_fresh(p, this_morning, NOW)
    assert not pol.is_fresh(p, yesterday_evening, NOW)


@pytest.mark.unit
def test_during_the_session_a_daily_response_ages_out_in_fifteen_minutes():
    p = pol.policy_for("TIME_SERIES_DAILY_ADJUSTED", {"symbol": "IBM"}, NOW)
    fetched = datetime(2026, 9, 25, 11, 0, tzinfo=NY)  # market open
    assert pol.is_fresh(p, fetched, fetched + timedelta(minutes=10))
    assert not pol.is_fresh(p, fetched, fetched + timedelta(minutes=20))


@pytest.mark.unit
def test_the_date_is_new_yorks_not_utcs():
    """21:00 in New York is already tomorrow in UTC; the day must not roll early."""
    p = pol.policy_for("OVERVIEW", {"symbol": "IBM"}, NOW)
    fetched = datetime(2026, 9, 25, 17, 0, tzinfo=NY)
    late = datetime(2026, 9, 25, 23, 30, tzinfo=NY)
    assert pol.is_fresh(p, fetched.astimezone(UTC), late.astimezone(UTC))


# --- through_store ---------------------------------------------------------------------


@pytest.mark.unit
def test_a_final_response_is_fetched_once(store_on):
    fetch, calls = _fetcher()
    params = {"symbol": "KO", "date": "2019-06-03"}
    for _ in range(3):
        assert ds.through_store("alpha_vantage", "HISTORICAL_OPTIONS", params, fetch) == "payload"
    assert len(calls) == 1


@pytest.mark.unit
def test_a_stale_daily_response_is_fetched_again(store_on):
    params = {"symbol": "IBM"}
    key = params_key(params)
    store_on.put("alpha_vantage", "OVERVIEW", key, "old", symbol="IBM", final=False,
                 fetched_at=datetime.now(UTC) - timedelta(days=2))
    fetch, calls = _fetcher("new")
    assert ds.through_store("alpha_vantage", "OVERVIEW", params, fetch) == "new"
    assert len(calls) == 1


@pytest.mark.unit
def test_replay_serves_what_is_stored_and_refuses_the_rest(store_on, monkeypatch):
    fetch, calls = _fetcher()
    ds.through_store("alpha_vantage", "HISTORICAL_OPTIONS", {"symbol": "KO", "date": "2019-06-03"}, fetch)
    monkeypatch.setenv("TRADINGAGENTS_DATA_STORE", "replay")
    assert ds.through_store("alpha_vantage", "HISTORICAL_OPTIONS",
                            {"symbol": "KO", "date": "2019-06-03"}, fetch) == "payload"
    with pytest.raises(ds.ReplayMissError):
        ds.through_store("alpha_vantage", "HISTORICAL_OPTIONS", {"symbol": "KO", "date": "2019-06-04"}, fetch)
    assert len(calls) == 1


@pytest.mark.unit
def test_off_bypasses_the_store_entirely(store_on, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_DATA_STORE", "off")
    fetch, calls = _fetcher()
    for _ in range(2):
        ds.through_store("alpha_vantage", "HISTORICAL_OPTIONS", {"symbol": "KO", "date": "2019-06-03"}, fetch)
    assert len(calls) == 2
    assert store_on.stats()["responses"] == 0


@pytest.mark.unit
def test_an_unknown_mode_fails_loudly(store_on, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_DATA_STORE", "sometimes")
    with pytest.raises(ValueError, match="data_store"):
        ds.through_store("alpha_vantage", "OVERVIEW", {"symbol": "IBM"}, _fetcher()[0])


@pytest.mark.unit
def test_a_vendor_error_is_never_stored(store_on):
    """Alpha Vantage sometimes answers a valid symbol with an error and is fine
    seconds later; callers retry, and a stored error would answer the retry."""
    params = {"symbol": "NOPE", "date": "2019-06-03"}
    fetch, calls = _fetcher('{"Error Message": "Invalid API call."}')
    ds.through_store("alpha_vantage", "HISTORICAL_OPTIONS", params, fetch)
    ds.through_store("alpha_vantage", "HISTORICAL_OPTIONS", params, fetch)
    assert store_on.get("alpha_vantage", "HISTORICAL_OPTIONS", params_key(params)) is None
    assert len(calls) == 2


@pytest.mark.unit
def test_a_failed_fetch_stores_nothing(store_on):
    def boom():
        raise RuntimeError("network")

    with pytest.raises(RuntimeError):
        ds.through_store("alpha_vantage", "OVERVIEW", {"symbol": "IBM"}, boom)
    assert store_on.stats()["responses"] == 0


@pytest.mark.unit
def test_hits_and_fetches_are_counted_per_day(store_on):
    fetch, _ = _fetcher()
    params = {"symbol": "KO", "date": "2019-06-03"}
    for _ in range(3):
        ds.through_store("alpha_vantage", "HISTORICAL_OPTIONS", params, fetch)
    day = datetime.now(UTC).astimezone(NY).date().isoformat()
    assert store_on.counts(day)[("alpha_vantage", "HISTORICAL_OPTIONS")] == {"fetch": 1, "hit": 2}


# --- the store itself -----------------------------------------------------------------------


@pytest.mark.unit
def test_the_api_key_never_reaches_the_key_or_the_file(tmp_path):
    key = params_key({"symbol": "IBM", "apikey": "SECRET123", "source": "trading_agents"})
    assert "SECRET123" not in key and "source" not in key
    assert params_key({"b": 1, "a": 2}) == params_key({"a": 2, "b": 1})


@pytest.mark.unit
def test_a_corrupt_row_is_a_miss_not_a_crash(tmp_path):
    store = DataStore(tmp_path / "s.sqlite")
    store._conn().execute(
        "INSERT INTO response VALUES ('v','E','k',NULL,?,1,?)",
        (datetime.now(UTC).isoformat(), b"not gzip"),
    )
    assert store.get("v", "E", "k") is None


@pytest.mark.unit
def test_an_unchanged_snapshot_is_stored_as_a_pointer(tmp_path):
    store = DataStore(tmp_path / "s.sqlite")
    for day, body in [("2026-09-01", "A"), ("2026-09-08", "A"), ("2026-09-15", "B"), ("2026-09-22", "B")]:
        store.snapshot("v", "INSTITUTIONAL_HOLDINGS", "k", body, symbol="IBM", fetched_on=day)
    stored = store._conn().execute(
        "SELECT fetched_on FROM snapshot WHERE payload IS NOT NULL ORDER BY fetched_on").fetchall()
    assert [r[0] for r in stored] == ["2026-09-01", "2026-09-15"]
    assert store.snapshots("v", "INSTITUTIONAL_HOLDINGS", "k") == [
        ("2026-09-01", "A"), ("2026-09-08", "A"), ("2026-09-15", "B"), ("2026-09-22", "B")]


@pytest.mark.unit
def test_snapshot_endpoints_build_history_through_the_store(store_on):
    fetch, _ = _fetcher('{"holdings": []}')
    ds.through_store("alpha_vantage", "INSTITUTIONAL_HOLDINGS", {"symbol": "IBM"}, fetch)
    assert len(store_on.snapshots("alpha_vantage", "INSTITUTIONAL_HOLDINGS", params_key({"symbol": "IBM"}))) == 1


# --- the throttle ---------------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


@pytest.mark.unit
def test_the_throttle_allows_a_burst_then_paces(tmp_path):
    store = DataStore(tmp_path / "s.sqlite")
    clock = _Clock()
    for _ in range(15):  # 10% of 150/min
        assert acquire(store._conn(), "av", 150, clock=clock, sleep=clock.sleep) == 0
    waited = acquire(store._conn(), "av", 150, clock=clock, sleep=clock.sleep)
    assert waited == pytest.approx(60 / 150)


@pytest.mark.unit
def test_two_processes_share_one_bucket(tmp_path):
    path = tmp_path / "s.sqlite"
    DataStore(path)
    a, b = (sqlite3.connect(path, isolation_level=None, timeout=30) for _ in range(2))
    clock = _Clock()
    for i in range(15):
        acquire(a if i % 2 else b, "av", 150, clock=clock, sleep=clock.sleep)
    assert acquire(a, "av", 150, clock=clock, sleep=clock.sleep) > 0, "the burst is shared, not per process"


# --- the Alpha Vantage client goes through it -------------------------------------------------


@pytest.mark.unit
def test_alpha_vantage_requests_go_through_the_store(store_on, monkeypatch):
    import tradingagents.dataflows.alpha_vantage_common as av

    response = MagicMock(text='{"data": [1]}')
    get = MagicMock(return_value=response)
    monkeypatch.setattr(av, "get_scrubbed", get)
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "SECRET123")
    params = {"symbol": "KO", "date": "2019-06-03"}
    assert av._make_api_request("HISTORICAL_OPTIONS", params) == '{"data": [1]}'
    assert av._make_api_request("HISTORICAL_OPTIONS", params) == '{"data": [1]}'
    assert get.call_count == 1
    raw = (store_on.path).read_bytes()
    assert b"SECRET123" not in raw


@pytest.mark.unit
def test_a_rate_limit_notice_is_raised_and_never_stored(store_on, monkeypatch):
    import tradingagents.dataflows.alpha_vantage_common as av

    notice = MagicMock(text='{"Information": "Thank you for using Alpha Vantage! rate limit"}')
    monkeypatch.setattr(av, "get_scrubbed", MagicMock(return_value=notice))
    with pytest.raises(av.AlphaVantageRateLimitError):
        av._make_api_request("OVERVIEW", {"symbol": "IBM"})
    assert store_on.stats()["responses"] == 0


# --- the nightly log's view -------------------------------------------------------------------


@pytest.mark.unit
def test_store_stats_reports_requests_served_and_fetched(store_on):
    from typer.testing import CliRunner

    import cli.main as m

    fetch, _ = _fetcher()
    for _ in range(4):
        ds.through_store("alpha_vantage", "HISTORICAL_OPTIONS", {"symbol": "KO", "date": "2019-06-03"}, fetch)
    result = CliRunner().invoke(m.app, ["store-stats"])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "1 responses (1 final)" in out
    assert "75% of requests served without a fetch" in out


@pytest.mark.unit
def test_store_stats_says_when_the_store_is_off():
    from typer.testing import CliRunner

    import cli.main as m

    result = CliRunner().invoke(m.app, ["store-stats"])
    assert result.exit_code == 0 and "off" in result.output
