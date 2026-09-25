"""Model usage per backtest cell: measured, so cost claims stop being estimates."""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

import tradingagents.backtest as bt
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.usage import Usage, UsageTracker, render_usage


def _response(tokens_in=100, tokens_out=20, cache_read=0, cache_write=0, meta=True):
    usage = {"input_tokens": tokens_in, "output_tokens": tokens_out,
             "total_tokens": tokens_in + tokens_out,
             "input_token_details": {"cache_read": cache_read, "cache_creation": cache_write}}
    msg = AIMessage(content="x", usage_metadata=usage if meta else None)
    return LLMResult(generations=[[ChatGeneration(message=msg)]])


@pytest.mark.unit
def test_tracker_sums_calls_tokens_and_cache_separately():
    t = UsageTracker()
    t.on_llm_end(_response(1000, 50, cache_read=800))
    t.on_llm_end(_response(200, 10, cache_write=150))
    assert t.snapshot() == Usage(llm_calls=2, tokens_in=1200, tokens_out=60,
                                 cache_read=800, cache_write=150)


@pytest.mark.unit
def test_a_call_without_usage_metadata_still_counts_as_a_call():
    t = UsageTracker()
    t.on_llm_end(_response(meta=False))
    assert t.snapshot() == Usage(llm_calls=1)


@pytest.mark.unit
def test_a_cell_is_the_difference_between_two_snapshots():
    t = UsageTracker()
    t.on_llm_end(_response(500, 5))
    before = t.snapshot()
    t.on_llm_end(_response(300, 7))
    assert t.snapshot() - before == Usage(llm_calls=1, tokens_in=300, tokens_out=7)


@pytest.mark.unit
def test_render_counts_only_cells_and_gives_a_per_cell_mean():
    rows = [
        {"kind": "cell", "llm_calls": 10, "tokens_in": 600_000, "tokens_out": 20_000,
         "cache_read": 400_000, "cache_write": 50_000},
        {"kind": "cell", "llm_calls": 12, "tokens_in": 400_000, "tokens_out": 10_000,
         "cache_read": 0, "cache_write": 0},
        {"kind": "settle", "llm_calls": 1, "tokens_in": 9, "tokens_out": 9,
         "cache_read": 0, "cache_write": 0},
    ]
    line = render_usage(rows)
    assert "2 cell(s)" in line and "1,000,000 tokens in" in line
    assert "per cell 500,000 in / 15,000 out" in line
    assert render_usage([]) == ""


class _SpendingGraph:
    """A graph whose cells and settlement make model calls through its callbacks."""

    fail_on: set = set()

    def __init__(self, selected_analysts=None, config=None, mandate=None, callbacks=None, **kw):
        self.callbacks = callbacks or []
        self.mandate_name = mandate or ""
        self.memory_log = TradingMemoryLog(config)

    def _spend(self, n_in):
        for cb in self.callbacks:
            cb.on_llm_end(_response(n_in, 10))

    def propagate(self, ticker, date, asset_type="stock", portfolio=None):
        self._spend(1000)
        if (ticker, date) in _SpendingGraph.fail_on:
            raise RuntimeError("vendor exploded")
        self.memory_log.store_decision(ticker, date, "Rating: Buy", mandate=self.mandate_name)

    def settle_pending(self, ticker):
        if ticker == "AAPL":
            self._spend(50)


@pytest.fixture
def spending(monkeypatch):
    _SpendingGraph.fail_on = set()
    monkeypatch.setattr(bt, "TradingAgentsGraph", _SpendingGraph)
    return _SpendingGraph


@pytest.mark.unit
def test_run_backtest_writes_one_row_per_cell_including_failures(tmp_path, spending):
    spending.fail_on = {("NVDA", "2026-01-12")}
    cfg = {"results_dir": str(tmp_path), "memory_log_path": str(tmp_path / "live.md")}
    result = bt.run_backtest(["NVDA", "AAPL"], ["2026-01-05", "2026-01-12"], cfg,
                             run_id="u1", mandate="equity_value")

    rows = [json.loads(line) for line in (tmp_path / "backtest" / "u1" / "usage.jsonl").open()]
    cells = [r for r in rows if r["kind"] == "cell"]
    assert [(r["ticker"], r["date"], r["status"]) for r in cells] == [
        ("NVDA", "2026-01-05", "ok"), ("NVDA", "2026-01-12", "failed"),
        ("AAPL", "2026-01-05", "ok"), ("AAPL", "2026-01-12", "ok"),
    ]
    assert all(r["tokens_in"] == 1000 and r["llm_calls"] == 1 for r in cells), "per cell, not cumulative"
    assert all(r["mandate"] == "equity_value" for r in rows)
    # Settlement is recorded only where it actually called a model.
    assert [(r["ticker"], r["tokens_in"]) for r in rows if r["kind"] == "settle"] == [("AAPL", 50)]
    assert result.usage == rows


@pytest.mark.unit
def test_a_resumed_run_appends_rather_than_overwrites(tmp_path, spending):
    cfg = {"results_dir": str(tmp_path), "memory_log_path": str(tmp_path / "live.md")}
    bt.run_backtest(["NVDA"], ["2026-01-05"], cfg, run_id="u2")
    bt.run_backtest(["NVDA"], ["2026-01-05", "2026-01-12"], cfg, run_id="u2")
    rows = [json.loads(line) for line in (tmp_path / "backtest" / "u2" / "usage.jsonl").open()]
    assert [r["date"] for r in rows if r["kind"] == "cell"] == ["2026-01-05", "2026-01-12"]
