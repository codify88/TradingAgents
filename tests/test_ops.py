"""Operator actions and watchers: bounded, refused at the wrong time, silent when idle."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from tradingagents.ops import jobs, watch


@pytest.fixture
def config(tmp_path):
    return {"results_dir": str(tmp_path)}


# --- actions ----------------------------------------------------------------------------


@pytest.mark.unit
def test_actions_are_refused_in_the_night_window_and_while_busy(config):
    idle = lambda: None  # noqa: E731
    assert "night window" in jobs.refusal(datetime(2026, 9, 26, 3, 0), busy=idle)
    assert jobs.refusal(datetime(2026, 9, 26, 12, 0), busy=idle) is None
    assert "in progress" in jobs.refusal(datetime(2026, 9, 26, 12, 0), busy=lambda: "tradingagents harvest")


@pytest.mark.unit
def test_start_launches_detached_and_records_the_job(config):
    launched = []
    msg = jobs.start(config, "screen-run 132108 (2 names)", ["screen-run", "132108", "--max-names", "2"],
                     now=datetime(2026, 9, 26, 12, 0), busy=lambda: None,
                     launcher=lambda *a, **k: launched.append((a, k)))
    assert msg.startswith("Started job 20260926-120000-")
    (args, kwargs), = launched
    assert kwargs["start_new_session"] is True, "detached: the MCP call returns at once"
    assert "screen-run 132108 --max-names 2" in args[0][2]
    meta = json.loads(next(jobs.jobs_dir(config).glob("*.json")).read_text())
    assert meta["kind"] == "screen-run 132108 (2 names)"


@pytest.mark.unit
def test_start_refuses_an_unexpected_argument(config):
    with pytest.raises(ValueError):
        jobs.start(config, "x", ["screen-run", "132108; rm -rf ~"], now=datetime(2026, 9, 26, 12, 0),
                   busy=lambda: None, launcher=lambda *a, **k: None)


@pytest.mark.unit
def test_status_reads_the_job_log(config):
    d = jobs.jobs_dir(config)
    d.mkdir(parents=True)
    (d / "20260926-120000-abc123.json").write_text(json.dumps(
        {"id": "20260926-120000-abc123", "kind": "screen-run 132108 (2 names)", "args": [],
         "started": "2026-09-26T12:00:00"}))
    (d / "20260926-120000-abc123.log").write_text("Ran 1 names, skipped 0.\nfailed: X 2019-09-03: boom\nexit=2\n")
    text = jobs.status(config)
    assert "finished, exit 2" in text and "names run 1, failed 1" in text


@pytest.mark.unit
def test_the_actions_are_registered_as_not_read_only():
    pytest.importorskip("mcp.server.mcpserver")
    import asyncio

    from tradingagents.mcp_server.server import build_server
    from tradingagents.mcp_server.tools import ACTION_TOOLS

    listed = {t.name: t for t in asyncio.run(build_server().list_tools())}
    for fn in ACTION_TOOLS:
        assert listed[fn.__name__].annotations.read_only_hint is False
    assert listed["job_status"].annotations.read_only_hint is True


@pytest.mark.unit
def test_screen_run_caps_the_names(monkeypatch, config):
    from types import SimpleNamespace

    from tradingagents.mcp_server import tools

    monkeypatch.setattr(tools, "_config", lambda: config)
    started = {}
    monkeypatch.setattr(jobs, "start", lambda cfg, kind, args, **k: started.update(args=args) or "ok")
    import tradingagents.screener.run as sr
    monkeypatch.setattr(sr, "find", lambda cfg, sid: SimpleNamespace(run_id="2019-09-03_equity_value_132108"))
    tools.screen_run("132108", max_names=50)
    assert started["args"] == ["screen-run", "132108", "--max-names", "5"]


# --- watchers --------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_earnings_watcher_speaks_only_about_open_decisions_this_week(monkeypatch):
    monkeypatch.setattr(watch, "open_positions", lambda config=None: {"NVDA", "KO"})
    cal = ("symbol,name,reportDate,fiscalDateEnding,estimate,currency\n"
           "NVDA,Nvidia,2026-09-29,2026-07-31,1.2,USD\n"
           "KO,Coca-Cola,2026-10-20,2026-09-30,0.7,USD\n"
           "AAPL,Apple,2026-09-28,2026-09-30,1.6,USD\n")
    text = watch.earnings(today="2026-09-26", calendar_csv=cal)
    assert "NVDA" in text and "KO" not in text and "AAPL" not in text
    assert watch.earnings(today="2026-11-30", calendar_csv=cal) == ""


@pytest.mark.unit
def test_the_reviews_watcher_is_silent_when_nothing_is_due(monkeypatch):
    import tradingagents.mcp_server.tools as tools

    monkeypatch.setattr(tools, "pending_reviews", lambda days, today=None: "No reviews due in the next 7 days.")
    assert watch.reviews() == ""
    monkeypatch.setattr(tools, "pending_reviews", lambda days, today=None: "Due in the next 7 days (1):\n- x")
    assert watch.reviews().startswith("Reviews this week")


@pytest.mark.unit
def test_the_ownership_watcher_reports_only_what_became_public_yesterday_or_today(monkeypatch, tmp_path):
    import tradingagents.datastore as dstore
    from tradingagents.datastore import graph

    monkeypatch.setenv("TRADINGAGENTS_DATA_STORE", "read_write")
    monkeypatch.setattr(dstore, "_config", lambda: {"data_cache_dir": str(tmp_path), "data_store_path": None,
                                                     "av_requests_per_minute": 0})
    dstore._stores.clear()
    store = dstore.get_store()
    graph.fill_congress(store, "NVDA", json.dumps({"trades": [
        {"bioguide_id": "S1", "politician_canonical": "Rep One", "party": "R", "transaction_date": "2026-09-01",
         "filed_date": "2026-09-25", "transaction_type": "SELL", "amount_min": "1001", "amount_max": "15000"},
        {"bioguide_id": "S2", "politician_canonical": "Rep Two", "party": "D", "transaction_date": "2026-06-01",
         "filed_date": "2026-07-01", "transaction_type": "BUY"}]}))
    monkeypatch.setattr(watch, "open_positions", lambda config=None: {"NVDA"})
    text = watch.ownership(today="2026-09-26")
    dstore._stores.clear()
    assert "Rep One" in text and "Rep Two" not in text
