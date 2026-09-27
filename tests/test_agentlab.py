"""The agent lab: catalog, variants, the call-time hook, suites, runs and metrics."""

from __future__ import annotations

import json
from typing import TypedDict

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from tradingagents.agentlab import catalog, hook, runs, suites, variants as va


@pytest.fixture
def config(tmp_path):
    return {"data_cache_dir": str(tmp_path / "cache"), "results_dir": str(tmp_path / "results"),
            "deep_think_llm": "claude-opus-4-8", "quick_think_llm": "claude-sonnet-5",
            "data_vendors": {"news_data": "yfinance", "ownership_data": "alpha_vantage"}, "tool_vendors": {}}


class Recorder(GenericFakeChatModel):
    """A chat model that remembers exactly what each call was sent."""

    seen: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        Recorder.seen.append(messages)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kw)


def _graph(llm):
    """Two nodes calling the model the way agents do: messages with a system
    prompt, and a bare string (as the researchers do)."""
    from langgraph.graph import END, START, StateGraph

    class S(TypedDict):
        x: int

    def pm(s):
        llm.invoke([SystemMessage("You are the portfolio manager. Rate it."), HumanMessage("Reports here.")])
        return {}

    def bull(s):
        llm.invoke("You are the bull researcher. Argue for it.")
        return {}

    g = StateGraph(S)
    g.add_node("Portfolio Manager", pm)
    g.add_node("Bull Researcher", bull)
    g.add_edge(START, "Portfolio Manager")
    g.add_edge("Portfolio Manager", "Bull Researcher")
    g.add_edge("Bull Researcher", END)
    return g.compile()


def test_the_catalog_lists_agents_tools_vendors_and_what_nothing_calls(config):
    c = catalog.catalog(config)
    nodes = [a["node"] for a in c["agents"]]
    assert nodes[0] == "Market Analyst" and nodes[-1] == "Portfolio Manager" and len(nodes) == 12
    pm = next(a for a in c["agents"] if a["node"] == "Portfolio Manager")
    assert pm["tier"] == "deep" and pm["model"] == "claude-opus-4-8" and pm["tools"] == []
    news = next(a for a in c["agents"] if a["node"] == "News Analyst")
    assert "get_macro_indicators" in news["tools"]
    tools = {t["name"]: t for t in c["tools"]}
    assert tools["get_news"]["vendor"] == "yfinance" and "News Analyst" in tools["get_news"]["used_by"]
    assert "get_congress_trades" in c["unused_tools"] and tools["get_congress_trades"]["vendor"] == "alpha_vantage"


def test_a_variant_is_validated_versioned_and_kept_in_history(config):
    bad = va.Variant("Bad Name", edits=[va.Edit("Nobody", "append", "x"), va.Edit("Trader", "replace", "y")],
                     settings={"max_debate_rounds": 0}, extra_tools={"trader": ["get_news"]})
    errs = va.validate(bad)
    assert any("name" in e for e in errs) and any("unknown agent" in e for e in errs)
    assert any("exact passage" in e for e in errs) and any("1 to 60" in e for e in errs)
    assert any("not an analyst" in e for e in errs)
    v = va.Variant("flat-book", "relative ratings",
                   edits=[va.Edit("Portfolio Manager", "append", "The book is in cash.")],
                   models={"deep": "claude-haiku-4-5", "quick": "claude-haiku-4-5"})
    assert va.save(config, v).version == 1
    v.description = "relative ratings, v2"
    assert va.save(config, v).version == 2
    assert [h["version"] for h in va.history(config, "flat-book")] == [1, 2]
    assert va.load(config, "flat-book").edits[0].text == "The book is in cash."
    assert [x.name for x in va.all_variants(config)] == ["baseline", "flat-book"]
    assert v.config_overrides() == {"deep_think_llm": "claude-haiku-4-5", "quick_think_llm": "claude-haiku-4-5"}
    with pytest.raises(ValueError, match="production"):
        va.save(config, va.Variant("baseline"))


def test_the_hook_edits_only_the_named_agent_and_captures_what_it_saw(tmp_path):
    Recorder.seen = []
    llm = Recorder(messages=iter([AIMessage("ok")] * 10))
    v = va.Variant("t", edits=[
        va.Edit("Portfolio Manager", "append", "HOLDING PERIOD: 5 days."),
        va.Edit("Portfolio Manager", "replace", "Rate this very carefully.", find="Rate it."),
        va.Edit("Bull Researcher", "prepend", "Be brief."),
        va.Edit("Bull Researcher", "replace", "nothing", find="a passage that is not there"),
    ])
    with hook.installed(v, capture=tmp_path / "cap.jsonl") as state:
        token = hook.CASE.set(("AAPL", "2025-09-05"))
        _graph(llm).invoke({"x": 0})
        hook.CASE.reset(token)
    pm_sys = Recorder.seen[0][0].content
    assert pm_sys == "You are the portfolio manager. Rate this very carefully.\n\nHOLDING PERIOD: 5 days."
    assert Recorder.seen[0][1].content == "Reports here."          # the human message is untouched
    assert Recorder.seen[1][0].content.startswith("Be brief.\n\nYou are the bull researcher")
    assert dict(state.missed) == {3: 1} and state.applied[0] == 1
    rows = [json.loads(x) for x in (tmp_path / "cap.jsonl").read_text().splitlines()]
    assert [r["node"] for r in rows] == ["Portfolio Manager", "Bull Researcher"]
    assert rows[0]["ticker"] == "AAPL" and "HOLDING PERIOD" in rows[0]["messages"][0]["text"]
    # Outside the block nothing is changed.
    Recorder.seen = []
    _graph(Recorder(messages=iter([AIMessage("ok")] * 4))).invoke({"x": 0})
    assert Recorder.seen[0][0].content == "You are the portfolio manager. Rate it."


def test_extra_tools_reach_the_named_analyst_only():
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    v = va.Variant("t", extra_tools={"news": ["get_congress_trades", "get_earnings_call"]})
    with hook.installed(v):
        nodes = TradingAgentsGraph._create_tool_nodes(None)
        assert {"get_congress_trades", "get_earnings_call"} <= set(nodes["news"].tools_by_name)
        assert "get_congress_trades" not in nodes["market"].tools_by_name
    assert "get_congress_trades" not in TradingAgentsGraph._create_tool_nodes(None)["news"].tools_by_name


def test_weekly_dates_and_a_cutoff_warning():
    days = suites.weekly_dates("2025-08-01", "2026-08-28", 5)
    assert len(days) == 5 and days[0] == "2025-08-01" and days[-1] == "2026-08-28"
    s = suites.Suite("s", "", "", ["2025-06-06", "2025-09-05"], 12, 8, 4)
    assert "trained on data to 2025-07-31" in suites.cutoff_warning(s, ["claude-haiku-4-5"])
    assert suites.cutoff_warning(s, ["claude-opus-5-5-x"]) is not None
    later = suites.Suite("s", "", "", ["2025-09-05"], 12, 8, 4)
    assert suites.cutoff_warning(later, ["claude-haiku-4-5"]) is None


def _manifest(path, day, picks, controls):
    from tradingagents.screener.manifest import ScreenManifest

    m = ScreenManifest(run_id=f"{day}_none_000001", mandate="", as_of=day, created="now", universe_size=10,
                       tiers=[], ordering_signal="x",
                       picks=[{"symbol": s, "rank": i + 1, "value": 1.0} for i, s in enumerate(picks)],
                       controls=[{"symbol": s, "value": 0.0} for s in controls], control_seed=1,
                       eligible_count=10, notes=[])
    path.parent.mkdir(parents=True, exist_ok=True)
    from dataclasses import asdict
    path.write_text(json.dumps(asdict(m)))
    return path


def _entry(t, rating, alpha, horizon="5 trading days"):
    return (f"[2025-09-05 | {t} | {rating} | {alpha} | {alpha} | 5d | resolved:2025-09-12]\n\nDECISION:\n"
            f"**Rating**: {rating}\n\n**Time Horizon**: {horizon}\n\n<!-- ENTRY_END -->\n\n")


def test_run_metrics_rating_mix_agents_edge_hit_rate_horizon_and_cost(config, tmp_path):
    f = _manifest(tmp_path / "src" / "a.json", "2025-09-05", ["AAA", "BBB"], ["CCC", "DDD"])
    s = suites.import_screens(config, "pilot", [f])
    assert s.cases == 4 and s.dates == ["2025-09-05"]
    v = va.Variant("cheap", models={"deep": "claude-haiku-4-5", "quick": "claude-haiku-4-5"})
    est = runs.estimate(config, v, "pilot")
    assert est["cases"] == 4 and est["cost_low"] == est["cost_high"] == pytest.approx(4 * (0.16 + 0.2))
    rid = runs.start_record(config, v, "pilot")
    d = runs._root(config) / rid
    (d / "backtest" / "x").mkdir(parents=True)
    (d / "backtest" / "x" / "trading_memory.md").write_text(
        _entry("AAA", "Buy", "+4.0%") + _entry("BBB", "Underweight", "-2.0%")
        + _entry("CCC", "Overweight", "+2.0%", "12 months") + _entry("DDD", "Hold", "-1.0%"))
    (d / "backtest" / "x" / "usage.jsonl").write_text("\n".join(json.dumps(
        {"kind": "cell", "tokens_in": 100_000, "tokens_out": 20_000, "llm_calls": 16}) for _ in range(4)))
    m = runs.metrics(config, rid)
    assert m["ratings"] == {"Buy": 1, "Overweight": 1, "Hold": 1, "Underweight": 1, "Sell": 0}
    assert m["agents"] == pytest.approx(0.03 - (-0.015))          # (+4%, +2%) vs (-2%, -1%)
    assert m["hit_rate"] == 1.0 and m["calls"] == 3                 # Hold not counted
    assert m["horizon_matched"] == 3 and m["horizon_stated"] == 4
    assert m["picks_vs_controls"] == pytest.approx(0.01 - 0.005)
    assert m["cost_low"] == pytest.approx(4 * (0.1 * 1 + 0.02 * 5))


def test_desk_saves_previews_and_guards_the_spend(config, tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from tradingagents.desk.app import create_app
    from tradingagents.ops import jobs

    started = []
    monkeypatch.setattr(jobs, "start", lambda cfg, kind, args: started.append(args) or f"Started job x: {kind}")
    c = TestClient(create_app(config, token="t"), base_url="http://localhost:8810", client=("127.0.0.1", 1))
    h = {"X-Desk-Token": "t", "Origin": "http://localhost:8810"}
    assert len(c.get("/api/v1/agents/catalog").json()["agents"]) == 12
    body = {"name": "flat-book", "edits": [{"agent": "Portfolio Manager", "kind": "append", "text": "In cash."}],
            "models": {"deep": "claude-haiku-4-5", "quick": "claude-haiku-4-5"}}
    assert c.post("/api/v1/agents/variants", json=body).status_code == 403           # token needed
    assert c.post("/api/v1/agents/variants", json=body, headers=h).json()["version"] == 1
    bad = c.post("/api/v1/agents/variants", json={**body, "extra_tools": {"news": ["nope"]}}, headers=h)
    assert bad.status_code == 400 and "unknown tool" in bad.json()["error"]

    # Preview: nothing captured yet, then a capture from a past run.
    pv = c.post("/api/v1/agents/preview", json={"variant": "flat-book", "node": "Portfolio Manager"}).json()
    assert pv["capture"] is None and "one case is enough" in pv["note"]
    rid = runs.start_record(config, va.load(config, "flat-book"), "pilot")
    (runs._root(config) / rid / "captures.jsonl").write_text(json.dumps({
        "at": "now", "node": "Portfolio Manager", "ticker": "AAA", "date": "2025-09-05", "model": "m",
        "applied": [], "missed": [], "messages": [{"role": "system", "text": "You rate."},
                                                  {"role": "human", "text": "Reports."}]}) + "\n")
    pv = c.post("/api/v1/agents/preview", json={"variant": "flat-book", "node": "Portfolio Manager"}).json()
    assert pv["before"][0]["text"] == "You rate." and pv["after"][0]["text"] == "You rate.\n\nIn cash."
    unsaved = {"name": "x", "edits": [{"agent": "Portfolio Manager", "kind": "replace", "find": "absent", "text": "y"}]}
    assert c.post("/api/v1/agents/preview", json={"variant": unsaved, "node": "Portfolio Manager"}).json()["missed"] == [0]

    # Spending: a suite, an estimate, then a run only with the estimate confirmed and under the cap.
    f = _manifest(tmp_path / "src" / "a.json", "2025-09-05", ["AAA", "BBB"], ["CCC"])
    suites.import_screens(config, "pilot", [f])
    est = c.post("/api/v1/agents/estimate", json={"variant": "flat-book", "suite": "pilot"}).json()
    assert est["cases"] == 3 and est["cost_high"] > 0 and est["max_cost"] == 50.0
    run = {"variant": "flat-book", "suite": "pilot"}
    assert c.post("/api/v1/agents/runs", json=run, headers=h).status_code == 400      # not confirmed
    ok = c.post("/api/v1/agents/runs", json={**run, "confirm_cost": est["cost_high"]}, headers=h)
    assert ok.status_code == 200 and started[-1][:2] == ["agents", "execute"]
    pricey = va.save(config, va.Variant("pricey", models={"deep": "claude-fable-5-1", "quick": "claude-fable-5-1"}))
    big = c.post("/api/v1/agents/estimate", json={"variant": pricey.name, "suite": "pilot"}).json()
    config["agentlab_max_cost"] = 1.0
    c2 = TestClient(create_app(config, token="t"), base_url="http://localhost:8810", client=("127.0.0.1", 1))
    over = c2.post("/api/v1/agents/runs", json={"variant": "pricey", "suite": "pilot",
                                                "confirm_cost": big["cost_high"]}, headers=h)
    assert over.status_code == 400 and "over the $1 cap" in over.json()["error"]
    assert c2.post("/api/v1/agents/runs", json={"variant": "pricey", "suite": "pilot", "over_cap": True,
                                                "confirm_cost": big["cost_high"]}, headers=h).status_code == 200
    assert any(r["id"] == rid for r in c.get("/api/v1/agents/runs").json()["runs"])


def test_a_suite_shows_while_it_builds_and_a_failed_build_says_why(config):
    from types import SimpleNamespace

    seen = []

    def screen(mandate, day, cfg, picks, controls, control_seed):
        s = next(x for x in suites.all_suites(config) if x.name == "wk")
        seen.append((s.status, s.done, s.total))                      # visible, with progress, while building
        from tradingagents.screener.manifest import ScreenManifest
        m = ScreenManifest(run_id=f"{day}_none_{len(seen):06d}", mandate="", as_of=day, created="now",
                           universe_size=10, tiers=[], ordering_signal="x",
                           picks=[{"symbol": "AAA", "rank": 1, "value": 1.0}], controls=[{"symbol": "BBB", "value": 0}],
                           control_seed=control_seed, eligible_count=2, notes=[])
        return SimpleNamespace(manifest=m)

    s = suites.build(config, "wk", ["2025-09-05", "2025-09-12"], screen=screen)
    assert seen == [("building", 0, 2), ("building", 1, 2)] and s.status == "ready" and s.cases == 4
    assert suites.exists(config, "wk")

    def broken(*a, **k):
        raise RuntimeError("vendor down")

    with pytest.raises(RuntimeError):
        suites.build(config, "bad", ["2025-09-05"], screen=broken)
    bad = next(x for x in suites.all_suites(config) if x.name == "bad")
    assert bad.status == "failed" and "vendor down" in bad.error
    with pytest.raises(ValueError, match="failed"):
        runs.estimate(config, va.Variant("x"), "bad")


def test_lab_jobs_count_as_busy():
    from tradingagents.ops import jobs

    assert jobs._BUSY.search("/Users/me/.local/bin/tradingagents agents suite-build wk --start 2025-01-01")
    assert jobs._BUSY.search("python /x/tradingagents agents execute flat-book--wk--20260926")
    assert not jobs._BUSY.search("tradingagents agents catalog")
