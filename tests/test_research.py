"""Desk research: every tool for any symbol, and agent runs outside the book."""

from __future__ import annotations

import json
import time

import pytest

from tradingagents.desk import research


@pytest.fixture
def config(tmp_path):
    return {"data_cache_dir": str(tmp_path / "cache"), "results_dir": str(tmp_path / "results"),
            "deep_think_llm": "claude-opus-4-8", "quick_think_llm": "claude-sonnet-5",
            "data_vendors": {}, "tool_vendors": {}}


def test_symbols_are_checked_and_indices_allowed():
    assert research.clean_symbol(" brk.b ") == "BRK.B"
    assert research.clean_symbol("^gspc") == "^GSPC"
    for bad in ("", "AAPL;rm", "../x", "A B", "^^X"):
        with pytest.raises(ValueError):
            research.clean_symbol(bad)


def test_an_index_skips_company_only_tools():
    company = {row[0] for row in research._plan("MSFT", "2026-09-25")}
    index = {row[0] for row in research._plan("^GSPC", "2026-09-25")}
    assert {"income", "transcript", "holders", "quality", "leaps"} <= company
    assert not index & {"income", "transcript", "holders", "quality", "leaps", "insiders"}
    assert {"prices", "news", "trend", "strength", "macro_cpi"} <= index


def test_ownership_tools_get_the_as_of_date():
    """They read it from the graph state otherwise, and print an empty date without it."""
    plan = {row[0]: row[4] for row in research._plan("MSFT", "2026-09-25")}
    for key in ("transcript", "insiders", "congress", "holders", "etfs"):
        assert plan[key]["trade_date"] == "2026-09-25"


@pytest.mark.parametrize("text,status", [
    ("# Rows\n| a | b |", "ok"),
    ("", "empty"),
    ("No earnings call for ^GSPC before 2026-09-25.", "empty"),
    ("DATA_UNAVAILABLE: optional macro_data could not be retrieved (FRED_API_KEY ...)", "unavailable"),
    ("NO_DATA_AVAILABLE: No usable market data for '^GSPC'", "unavailable"),
    ("## Estimate revisions: ^GSPC\n\nUNAVAILABLE: no forward estimates", "unavailable"),
    ("<Yahoo Finance news unavailable for 2026-09-18..2026-09-25>", "unavailable"),
    ("Error: rate limited", "error"),
])
def test_status_reads_past_a_heading(text, status):
    assert research.status_of(text) == status


def test_fetch_saves_every_section_and_one_failure_is_not_the_fetch(config):
    def invoke(tool, args):
        if tool == "get_news":
            raise RuntimeError("down")
        return f"# {tool}\nrows for {args.get('ticker') or args.get('symbol')}"

    tools = {row[3]: None for row in research._plan("MSFT", "2026-09-25")}
    path = research.fetch(config, "msft", "2026-09-25", tools=tools, invoke=invoke)
    data = json.loads(path.read_text())
    by = {s["key"]: s for s in data["sections"]}
    assert data["status"] == "finished" and by["news"]["status"] == "error" and "down" in by["news"]["text"]
    assert by["income"]["status"] == "ok" and "MSFT" in by["income"]["text"]
    assert research.overview(config, "MSFT")["fetches"][0]["counts"]["error"] == 1


def test_a_run_builds_its_own_graph_and_keeps_its_report(config):
    seen = {}

    class Graph:
        def propagate(self, symbol, day):
            return {"company_of_interest": symbol, "trade_date": day}, "Overweight"

    def factory(cfg, mandate):
        seen.update(cfg=cfg, mandate=mandate)
        return Graph()

    rid = research.start_run(config, "AAPL", "2026-09-25", "equity_value", {"deep": "claude-haiku-4-5"})
    meta = research.execute_run(config, "AAPL", rid, graph_factory=factory)
    assert meta["status"] == "finished" and meta["rating"] == "Overweight"
    assert seen["mandate"] == "equity_value" and seen["cfg"]["deep_think_llm"] == "claude-haiku-4-5"
    assert seen["cfg"]["results_dir"].endswith(rid) and seen["cfg"]["checkpoint_enabled"] is False
    assert research.runs(config, "AAPL")[0]["id"] == rid
    with pytest.raises(ValueError):
        research.start_run(config, "AAPL", "2026-09-25", "made_up", None)


def test_desk_fetches_and_guards_the_spend(config, monkeypatch):
    from starlette.testclient import TestClient

    from tradingagents.desk.app import create_app
    from tradingagents.ops import jobs

    started, fetched = [], []
    monkeypatch.setattr(jobs, "start", lambda cfg, kind, args, **kw: started.append((args, kw)) or f"Started job x: {kind}")
    monkeypatch.setattr(research, "fetch", lambda cfg, s, d, **kw: fetched.append((s, d)))
    c = TestClient(create_app(config, token="t"), base_url="http://localhost:8810", client=("127.0.0.1", 1))
    h = {"X-Desk-Token": "t", "Origin": "http://localhost:8810"}

    assert c.get("/api/v1/research/search", params={"q": "S&P"}).json()["results"][0]["symbol"] == "^GSPC"
    assert "equity_value" in c.get("/api/v1/research/meta").json()["mandates"]
    assert c.get("/api/v1/research/bad;sym").status_code == 400

    assert c.post("/api/v1/research/MSFT/fetch", json={"date": "2026-09-25"}).status_code == 403
    assert c.post("/api/v1/research/MSFT/fetch", json={"date": "2999-01-01"}, headers=h).status_code == 400
    assert c.post("/api/v1/research/%5EGSPC/fetch", json={"date": "2026-09-25"}, headers=h).status_code == 200
    for _ in range(50):
        if fetched:
            break
        time.sleep(0.02)
    assert fetched == [("^GSPC", "2026-09-25")]
    assert c.get("/api/v1/research/MSFT/data/2026-09-25").status_code == 404

    est = c.post("/api/v1/research/estimate", json={"models": {"deep": "claude-haiku-4-5", "quick": "claude-haiku-4-5"}}).json()
    assert est["models"] == ["claude-haiku-4-5"] and est["cost_high"] == pytest.approx(0.36)
    run = {"date": "2026-09-25", "mandate": "equity_value", "models": {"deep": "claude-haiku-4-5", "quick": "claude-haiku-4-5"}}
    assert c.post("/api/v1/research/MSFT/runs", json=run).status_code == 403
    assert c.post("/api/v1/research/MSFT/runs", json=run, headers=h).status_code == 400         # not confirmed
    assert c.post("/api/v1/research/MSFT/runs", json={**run, "mandate": "x", "confirm_cost": 1}, headers=h).status_code == 400
    ok = c.post("/api/v1/research/MSFT/runs", json={**run, "confirm_cost": est["cost_high"]}, headers=h).json()
    args, kw = started[-1]
    assert args == ["research", "execute", "MSFT", ok["run"]] and kw["busy"]() is None   # other jobs don't block it
    assert c.get(f"/api/v1/research/MSFT/runs/{ok['run']}").json()["mandate"] == "equity_value"
    assert c.get("/api/v1/research/MSFT").json()["runs"][0]["id"] == ok["run"]

    # A refused job (the night window) leaves no run behind.
    monkeypatch.setattr(jobs, "start", lambda cfg, kind, args, **kw: "Refused: 02:00 is inside the night window")
    night = c.post("/api/v1/research/AAPL/runs", json={**run, "confirm_cost": est["cost_high"]}, headers=h).json()
    assert night["result"].startswith("Refused") and research.runs(config, "AAPL") == []


def test_job_arguments_allow_an_index():
    from tradingagents.ops.jobs import _quote

    assert _quote("^GSPC") == "^GSPC"
    with pytest.raises(ValueError):
        _quote("^GS PC")
