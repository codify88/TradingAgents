"""The operator's read tools: what they answer, and what they must never expose."""
from __future__ import annotations

import inspect
import json
import shutil
from pathlib import Path

import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.mcp_server import tools

FIX = Path(__file__).parent / "fixtures" / "nightly"


@pytest.fixture
def results(tmp_path, monkeypatch):
    root = tmp_path / "logs"
    (root / "nightly").mkdir(parents=True)
    for f in FIX.glob("*.log"):
        shutil.copy(f, root / "nightly" / f.name)
    monkeypatch.setattr(tools, "_config", lambda: {
        "results_dir": str(root), "memory_log_path": str(tmp_path / "memory" / "trading_memory.md")})
    return root


def _sweep(root, run_id, *decisions):
    log = TradingMemoryLog({"memory_log_path": str(root / "backtest" / run_id / "trading_memory.md")})
    for ticker, day, mandate in decisions:
        log.store_decision(ticker, day, "**Rating**: Buy\n\nthesis", mandate=mandate)
    return log


@pytest.mark.unit
def test_nightly_status_reads_the_latest_run_and_names_its_problems(results):
    text = tools.nightly_status()
    assert "2026-09-25" in text and "asleep" in text
    assert "9 cell(s) failed" in tools.nightly_status("2026-09-23")
    assert "No nightly run found for 2026-01-01" in tools.nightly_status("2026-01-01")


@pytest.mark.unit
def test_list_decisions_filters_and_orders_newest_first(results):
    _sweep(results, "scr_a", ("PYPL", "2019-09-03", "equity_value"), ("SBUX", "2019-09-03", "equity_value"))
    _sweep(results, "scr_b", ("NVDA", "2026-09-17", "equity_momentum"))
    text = tools.list_decisions()
    assert text.index("NVDA") < text.index("PYPL")
    assert "PYPL" not in tools.list_decisions(mandate="equity_momentum")
    assert "SBUX" in tools.list_decisions(ticker="sbux") and "PYPL" not in tools.list_decisions(ticker="sbux")
    assert tools.list_decisions(since="2027-01-01") == "No decisions match."


@pytest.mark.unit
def test_pending_reviews_finds_the_next_outstanding_horizon(results):
    _sweep(results, "scr_b", ("NVDA", "2026-09-17", "equity_momentum"))
    text = tools.pending_reviews(within_days=60, today="2026-09-25")
    assert "2026-10-16 NVDA" in text and "21-day review" in text
    assert "No reviews due" in tools.pending_reviews(within_days=5, today="2026-09-25")
    assert "Overdue" in tools.pending_reviews(within_days=5, today="2026-11-01")


@pytest.mark.unit
def test_get_report_assembles_the_reports_and_caps_the_length(results, monkeypatch):
    logs = results / "backtest" / "scr_a" / "PYPL" / "TradingAgentsStrategy_logs"
    logs.mkdir(parents=True)
    (logs / "full_states_log_2019-09-03.json").write_text(json.dumps({
        "market_report": "trend down", "final_trade_decision": "Rating: Hold",
        "mandate_reports": {"Valuation": "cheap vs own history"}}))
    text = tools.get_report("pypl", "2019-09-03")
    assert "## Market\ntrend down" in text and "## Valuation" in text and "scr_a" in text
    monkeypatch.setattr(tools, "MAX_REPORT_CHARS", 20)
    assert "cut at 20 characters" in tools.get_report("PYPL", "2019-09-03")
    assert tools.get_report("PYPL", "2019-09-04") == "No report for PYPL on 2019-09-04."


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["../etc", "A*", "PY/PL"])
def test_get_report_refuses_anything_that_is_not_a_ticker(results, ticker):
    assert tools.get_report(ticker, "2019-09-03").startswith("Not a ticker")


@pytest.mark.unit
def test_get_report_refuses_a_non_date(results):
    with pytest.raises(ValueError):
        tools.get_report("PYPL", "../../x")


@pytest.mark.unit
def test_no_tool_takes_a_config_or_a_path():
    """A tool's parameters are what any MCP client may send; where to read must
    not be one of them."""
    for fn in tools.READ_TOOLS:
        params = set(inspect.signature(fn).parameters)
        assert not params & {"config", "path", "results_dir", "log_dir"}, fn.__name__


@pytest.mark.unit
def test_no_tool_answer_carries_a_key(results, monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "SECRETKEY123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET")
    _sweep(results, "scr_a", ("PYPL", "2019-09-03", "equity_value"))
    answers = [tools.nightly_status(), tools.list_decisions(), tools.pending_reviews(),
               tools.get_report("PYPL", "2019-09-03"), tools.data_store_stats()]
    for text in answers:
        assert "SECRETKEY123" not in text and "sk-ant-SECRET" not in text


@pytest.mark.unit
def test_the_server_registers_every_read_tool_as_read_only():
    pytest.importorskip("mcp.server.mcpserver")
    import asyncio

    from tradingagents.mcp_server.server import build_server

    listed = asyncio.run(build_server().list_tools())
    names = {t.name for t in listed}
    assert names == {fn.__name__ for fn in tools.READ_TOOLS}
    assert all(t.annotations and t.annotations.read_only_hint for t in listed)
