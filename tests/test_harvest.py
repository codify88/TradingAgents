"""The harvest: what is due, in what order, within what budget -- and each fact
dated by when it became public."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

import tradingagents.datastore as dstore
from tradingagents.datastore import graph
from tradingagents.datastore.store import params_key
from tradingagents.harvest import datasets as ds, run as hv


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_DATA_STORE", "read_write")
    cfg = {"data_cache_dir": str(tmp_path / "cache"), "data_store_path": None,
           "av_requests_per_minute": 0, "results_dir": str(tmp_path / "logs"),
           "memory_log_path": str(tmp_path / "memory.md")}
    monkeypatch.setattr(dstore, "_config", lambda: cfg)
    monkeypatch.setattr(hv, "_config", lambda: cfg)
    dstore._stores.clear()
    s = dstore.get_store()
    s.cfg = cfg
    yield s
    dstore._stores.clear()


NOW = datetime(2026, 9, 26, 7, 0, tzinfo=UTC)  # 03:00 in New York


def _put(store, endpoint, params, body, *, age=timedelta(0), final=False):
    store.put(ds.VENDOR, endpoint, params_key(params), body if isinstance(body, str) else json.dumps(body),
              symbol=params.get("symbol"), final=final, fetched_at=NOW - age)


# --- the rules --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("end,fye,label", [
    ("2024-12-31", 9, "2025Q1"),   # checked against Alpha Vantage on AAPL
    ("2024-09-30", 9, "2024Q4"),   # checked against Alpha Vantage on AAPL
    ("2024-06-30", 9, "2024Q3"),
    ("2024-03-31", 12, "2024Q1"),
    ("2026-05-31", 11, "2026Q2"),
    ("2024-02-03", 1, "2024Q4"),   # a 52/53-week year ending early in February
])
def test_fiscal_quarter_labels(end, fye, label):
    assert ds.fiscal_quarter(end, fye) == label


@pytest.mark.unit
def test_public_when_rules():
    assert ds.insider_public("2026-09-25") == "2026-09-29"      # Friday -> Tuesday
    assert ds.insider_cutoff("2025-06-01") == "2025-05-29"      # Sunday -> Thursday's trades
    assert ds.thirteen_f_public("2026-06-30") == "2026-08-14"
    assert ds.fiscal_year_end_month("September") == 9 and ds.fiscal_year_end_month(None) == 12


@pytest.mark.unit
def test_the_stop_time():
    at = lambda h, m=0: datetime(2026, 9, 26, h, m).astimezone()  # noqa: E731
    assert hv._stop_time("07:30", at(3, 15)) == at(7, 30)
    assert hv._stop_time("07:30", at(8, 10)) == at(8, 10), "a late morning run stops at once"
    assert hv._stop_time("07:30", at(21, 0)) == at(7, 30) + timedelta(days=1)
    assert hv._stop_time(None, at(3)) is None


# --- what is due, and in what order -------------------------------------------------------


def _earnings(*quarters):
    return {"quarterlyEarnings": [{"fiscalDateEnding": e, "reportedDate": r} for e, r in quarters]}


@pytest.mark.unit
def test_transcript_quarters_use_the_fiscal_year_and_only_calls_already_held(store):
    _put(store, "EARNINGS", {"symbol": "AAPL"}, _earnings(
        ("2026-06-30", "2026-07-30"), ("2025-12-31", "2026-01-29"), ("2009-12-31", "2010-01-25"),
        ("2026-09-30", "2026-10-29")))  # not held yet
    _put(store, "OVERVIEW", {"symbol": "AAPL"}, {"FiscalYearEnd": "September"})
    assert hv.transcript_quarters(store, "AAPL", "2026-09-26") == [("2026Q3", "2026-07-30"),
                                                                  ("2026Q1", "2026-01-29")]


@pytest.mark.unit
def test_an_empty_transcript_is_asked_again_weekly_until_it_settles(store):
    p = {"symbol": "X", "quarter": "2026Q2"}
    _put(store, "EARNINGS_CALL_TRANSCRIPT", p, {"transcript": []}, age=timedelta(days=2))
    assert not hv._transcript_due(store, "X", "2026Q2", NOW)
    _put(store, "EARNINGS_CALL_TRANSCRIPT", p, {"transcript": []}, age=timedelta(days=8))
    assert hv._transcript_due(store, "X", "2026Q2", NOW)
    _put(store, "EARNINGS_CALL_TRANSCRIPT", p, {"transcript": []}, age=timedelta(days=400), final=True)
    assert not hv._transcript_due(store, "X", "2026Q2", NOW), "a settled empty answer is never re-asked"


@pytest.mark.unit
def test_holdings_go_first_and_priority_names_lead_each_stage(store):
    _put(store, "EARNINGS", {"symbol": "PRIO"}, _earnings(("2026-06-30", "2026-07-30")))
    _put(store, "EARNINGS", {"symbol": "B"}, _earnings(("2026-06-30", "2026-07-30"), ("2026-03-31", "2026-04-30")))
    order = [(t.stage, t.symbol) for t in hv.tasks(store, ["A", "B"], ["SPY"], ["PRIO"], NOW)]
    stages = [s for s, _ in order]
    assert stages[0] == "politicians"
    assert stages.index("holdings") < stages.index("earnings") < stages.index("etf profiles") \
        < stages.index("insider") < stages.index("transcripts (priority)") < stages.index("transcripts")
    assert [sym for s, sym in order if s == "holdings"] == ["PRIO", "A", "B"]
    tx = [(s, sym) for s, sym in order if s.startswith("transcripts")]
    assert tx[0] == ("transcripts (priority)", "PRIO")


@pytest.mark.unit
def test_nothing_fresh_is_due_again(store):
    fresh = timedelta(hours=1)
    store.snapshot(ds.VENDOR, "INSTITUTIONAL_HOLDINGS", params_key({"symbol": "A"}), "{}",
                   symbol="A", fetched_on="2026-09-26")
    store.snapshot(ds.VENDOR, "ETF_PROFILE", params_key({"symbol": "SPY"}), "{}",
                   symbol="SPY", fetched_on="2026-09-26")
    for ep in ("EARNINGS", "OVERVIEW", "INSIDER_TRANSACTIONS", "CONGRESS_TRADES"):
        _put(store, ep, {"symbol": "A"}, {}, age=fresh)
    _put(store, "POLITICIAN_METADATA", {}, {}, age=fresh)
    assert list(hv.tasks(store, ["A"], ["SPY"], [], NOW)) == []


@pytest.mark.unit
def test_small_etfs_are_refreshed_monthly_large_ones_weekly(store, monkeypatch):
    monkeypatch.setattr(ds, "WEEKLY_ETFS", 1)
    for e, assets in (("BIG", 9e11), ("SMALL", 1e7)):
        store.snapshot(ds.VENDOR, "ETF_PROFILE", params_key({"symbol": e}), "{}", symbol=e, fetched_on="2026-09-10")
        store.upsert_node(f"etf:{e}", "etf", {"net_assets": assets})
    due = [t.symbol for t in hv.tasks(store, [], ["BIG", "SMALL"], [], NOW) if t.stage == "etf profiles"]
    assert due == ["BIG"]


# --- running within a budget -----------------------------------------------------------------


@pytest.fixture
def fake_vendor(monkeypatch, store):
    import tradingagents.dataflows.alpha_vantage_common as av

    calls = []
    script = {}

    def fetch(function_name, params):
        calls.append((function_name, dict(params)))
        action = script.get(function_name)
        if callable(action):
            return action(params)
        return json.dumps({"ok": True})

    monkeypatch.setattr(av, "_fetch", fetch)
    monkeypatch.setattr(hv, "universe", lambda store, today, config=None: (["A", "B"], []))
    monkeypatch.setattr(hv, "priority_names", lambda config=None: [])
    return calls, script


@pytest.mark.unit
def test_a_run_stops_at_its_budget_and_the_next_continues(store, fake_vendor):
    calls, _ = fake_vendor
    first = hv.run(3, config=store.cfg)
    assert first.requests == 3 and first.stopped.startswith("budget")
    second = hv.run(3, config=store.cfg)
    asked_first, asked_second = calls[:3], calls[3:6]
    assert not set(map(str, asked_first)) & set(map(str, asked_second)), "nothing is asked twice"
    runs = (hv.harvest_dir(store.cfg) / "runs.jsonl").read_text().splitlines()
    assert len(runs) == 2 and json.loads(runs[0])["requests"] == 3
    assert second.requests == 3


@pytest.mark.unit
def test_a_rate_limit_pauses_once_then_stops(store, fake_vendor):
    import tradingagents.dataflows.alpha_vantage_common as av

    calls, script = fake_vendor

    def limited(params):
        raise av.AlphaVantageRateLimitError("rate limit")

    script["POLITICIAN_METADATA"] = limited
    pauses = []
    report = hv.run(100, config=store.cfg, sleep=pauses.append)
    assert pauses == [hv.RATE_LIMIT_PAUSE_SECONDS]
    assert report.stopped.startswith("rate limit") and report.requests == 0


@pytest.mark.unit
def test_repeated_vendor_failures_stop_the_run(store, fake_vendor, monkeypatch):
    calls, script = fake_vendor
    monkeypatch.setattr(hv, "MAX_CONSECUTIVE_FAILURES", 3)

    def boom(params):
        raise RuntimeError("503")

    for ep in ("POLITICIAN_METADATA", "INSTITUTIONAL_HOLDINGS"):
        script[ep] = boom
    report = hv.run(100, config=store.cfg)
    assert report.stopped == "3 vendor failures in a row" and len(report.failures) == 3


@pytest.mark.unit
def test_a_response_already_fresh_costs_no_budget(store, fake_vendor, monkeypatch):
    calls, _ = fake_vendor
    # Due by the harvest's weekly rule is decided from the store; a fresh row is not due at all.
    _put(store, "POLITICIAN_METADATA", {}, {}, age=timedelta(minutes=1))
    report = hv.run(2, config=store.cfg)
    assert ("POLITICIAN_METADATA", {}) not in calls
    assert report.requests == 2


@pytest.mark.unit
def test_a_dry_run_asks_nothing_and_records_nothing(store, fake_vendor):
    calls, _ = fake_vendor
    report = hv.run(1000, dry_run=True, config=store.cfg)
    assert calls == [] and report.requests > 0
    assert not (hv.harvest_dir(store.cfg) / "runs.jsonl").exists()


# --- the graph ---------------------------------------------------------------------------------


@pytest.mark.unit
def test_insider_edges_are_public_two_business_days_later_and_same_day_trades_both_kept(store):
    body = json.dumps({"data": [
        {"transaction_date": "2026-09-25", "executive": "Krishna, Arvind", "shares": "10"},
        {"transaction_date": "2026-09-25", "executive": "KRISHNA, ARVIND", "shares": "20"},
    ]})
    assert graph.fill_insider(store, "IBM", body) == 2
    assert store.edges_to("ticker:IBM", "traded", "2026-09-28") == [], "not public until Tuesday"
    assert len(store.edges_to("ticker:IBM", "traded", "2026-09-29")) == 2
    assert store.node("insider:KRISHNA ARVIND")["kind"] == "insider"


@pytest.mark.unit
def test_congress_edges_are_dated_by_filing_and_keep_the_fuller_politician_record(store):
    graph.fill_politicians(store, json.dumps({"politicians": [
        {"bioguide_id": "S000250", "display_name": "Pete Sessions", "aliases": ["pete sessions"]}]}))
    graph.fill_congress(store, "AAPL", json.dumps({"trades": [
        {"bioguide_id": "S000250", "politician_canonical": "Pete Sessions", "transaction_date": "2026-08-20",
         "filed_date": "2026-09-17", "transaction_type": "SELL"}]}))
    assert store.edges_to("ticker:AAPL", "traded", "2026-09-16") == []
    assert store.edges_to("ticker:AAPL", "traded", "2026-09-17")[0]["type"] == "SELL"
    assert store.node("politician:S000250")["aliases"] == ["pete sessions"], "metadata not overwritten"


@pytest.mark.unit
def test_the_graph_keeps_only_the_largest_holders(store, monkeypatch):
    monkeypatch.setattr(graph, "INSTITUTIONS_PER_TICKER", 2)
    holders = [{"holder_name": f"FUND {i} INC", "shares_held": str(i), "last_reported": "2026-06-30"}
               for i in range(1, 6)]
    graph.fill_institutional(store, "NVDA", json.dumps({"holdings": holders}))
    held = store.edges_to("ticker:NVDA", "holds")
    assert sorted(e["src"] for e in held) == ["institution:FUND 4", "institution:FUND 5"]
    assert held[0]["available_at"] == "2026-08-14", "13F public 45 days after the quarter"
    # A later snapshot of the same quarter replaces the top holders rather than adding to them.
    graph.fill_institutional(store, "NVDA", json.dumps({"holdings": holders[:2]}))
    assert sorted(e["src"] for e in store.edges_to("ticker:NVDA", "holds")) == ["institution:FUND 1",
                                                                                "institution:FUND 2"]


@pytest.mark.unit
def test_name_normalising_only_strips_legal_forms():
    assert graph.normalise_name("Vanguard Group, Inc.") == "VANGUARD GROUP"
    assert graph.normalise_name("BlackRock Inc") == graph.normalise_name("BLACKROCK, INC.")
    assert graph.normalise_name("Goldman Sachs Group") != graph.normalise_name("Goldman Sachs Asset Mgmt")


# --- the point-in-time tools -------------------------------------------------------------------


@pytest.mark.unit
def test_congress_trades_show_only_what_was_filed_by_the_date(store, monkeypatch):
    from tradingagents.dataflows import ownership

    body = json.dumps({"trades": [
        {"filed_date": "2026-09-17", "transaction_date": "2026-08-20", "politician_canonical": "A"},
        {"filed_date": "2026-08-01", "transaction_date": "2026-07-01", "politician_canonical": "B"}]})
    monkeypatch.setattr(ownership, "_make_api_request", lambda *a: body)
    out = ownership.get_congress_trades("AAPL", "2026-09-01")
    assert "| 2026-08-01 |" in out and "2026-09-17" not in out


@pytest.mark.unit
def test_holdings_before_the_first_snapshot_are_unknown_not_empty(store, monkeypatch):
    from tradingagents.dataflows import ownership

    monkeypatch.setattr(ownership, "_today", lambda: "2026-09-26")
    monkeypatch.setattr(ownership, "_make_api_request", lambda *a: pytest.fail("must not fetch"))
    out = ownership.get_institutional_holdings("NVDA", "2025-01-02")
    assert "keeps no history" in out


@pytest.mark.unit
def test_holdings_come_from_the_snapshot_on_or_before_the_date_and_13f_timing(store, monkeypatch):
    from tradingagents.dataflows import ownership

    snap = json.dumps({"holdings": [
        {"holder_name": "OLD Q", "shares_held": "5", "last_reported": "2026-03-31"},
        {"holder_name": "NEW Q", "shares_held": "9", "last_reported": "2026-06-30"}]})
    store.snapshot(ds.VENDOR, "INSTITUTIONAL_HOLDINGS", params_key({"symbol": "NVDA"}), snap,
                   symbol="NVDA", fetched_on="2026-08-01")
    out = ownership.get_institutional_holdings("NVDA", "2026-08-10")
    assert "OLD Q" in out and "NEW Q" not in out, "the June 13F was not due until 2026-08-14"


@pytest.mark.unit
def test_only_and_skip_restrict_the_stages(store, fake_vendor):
    calls, _ = fake_vendor
    report = hv.run(100, config=store.cfg, only={"insider"})
    assert set(report.by_stage) == {"insider"} and {c[0] for c in calls} == {"INSIDER_TRANSACTIONS"}
    calls.clear()
    report = hv.run(100, config=store.cfg, skip={"holdings", "politicians"})
    assert "holdings" not in report.by_stage and "politicians" not in report.by_stage
