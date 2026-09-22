"""Backtesting: many single-shot decisions, scored by the decision log.

A run already records its rating and later settles it with realized and alpha
return against the regional benchmark. A backtest is that machinery over a grid
of tickers and dates, aggregated. It evaluates decision quality; it does not
simulate a portfolio, so there is no execution, no fees and no equity curve.
"""

from __future__ import annotations

import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.backtest import iter_grid, run_backtest, summarize

DECISION = "Rating: Buy\n\nbuy it"


@pytest.mark.unit
def test_grid_spacing_and_canonical_dates():
    assert iter_grid("2026-01-05", "2026-01-20", every_n_days=7) == ["2026-01-05", "2026-01-12", "2026-01-19"]


@pytest.mark.unit
def test_grid_stops_at_today(monkeypatch):
    import tradingagents.backtest as bt

    monkeypatch.setattr(bt, "get_current_date", lambda: "2026-01-10")
    assert iter_grid("2026-01-05", "2026-02-20", every_n_days=5) == ["2026-01-05", "2026-01-10"]


@pytest.mark.unit
def test_grid_rejects_a_non_canonical_date():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        iter_grid("2026-1-5", "2026-01-20")


class _FakeGraph:
    """Stands in for TradingAgentsGraph, writing to the log the harness gave it."""

    instances: list = []
    fail_on: set = set()

    def __init__(self, selected_analysts=None, config=None, mandate=None, **kw):
        self.analysts = list(selected_analysts) if selected_analysts else None
        self.config = config
        self.mandate_arg = mandate
        # Mirrors TradingAgentsGraph: an explicit mandate wins, else the config's.
        self.mandate_name = (mandate if mandate is not None else config.get("mandate", "")) or ""
        self.memory_log = TradingMemoryLog(config)
        self.calls = []
        self.settled = []
        _FakeGraph.instances.append(self)

    def propagate(self, ticker, trade_date, asset_type="stock", portfolio=None):
        self.calls.append((ticker, trade_date))
        if (ticker, trade_date) in _FakeGraph.fail_on:
            raise RuntimeError("vendor exploded")
        self.memory_log.store_decision(ticker, trade_date, DECISION, mandate=self.mandate_name)
        return {"final_trade_decision": DECISION}, "Buy"

    def settle_pending(self, ticker):
        self.settled.append(ticker)


@pytest.fixture(autouse=True)
def _fake_graph(monkeypatch, tmp_path):
    import tradingagents.backtest as bt

    _FakeGraph.instances = []
    _FakeGraph.fail_on = set()
    monkeypatch.setattr(bt, "TradingAgentsGraph", _FakeGraph)
    return _FakeGraph


def _config(tmp_path):
    return {"results_dir": str(tmp_path / "results"),
            "memory_log_path": str(tmp_path / "live_trading_memory.md")}


@pytest.mark.unit
def test_the_live_decision_log_is_never_written(tmp_path):
    config = _config(tmp_path)
    result = run_backtest(["NVDA"], ["2026-01-05", "2026-01-12"], config)

    assert not (tmp_path / "live_trading_memory.md").exists()
    assert result.log_path.exists() and result.cells_run == 2


@pytest.mark.unit
def test_a_cell_already_in_the_log_is_not_run_again(tmp_path):
    config = _config(tmp_path)
    first = run_backtest(["NVDA"], ["2026-01-05"], config)

    again = run_backtest(["NVDA"], ["2026-01-05", "2026-01-12"], config, run_id=first.run_id)

    assert again.cells_run == 1 and again.skipped == 1
    assert _FakeGraph.instances[-1].calls == [("NVDA", "2026-01-12")]


@pytest.mark.unit
def test_every_ticker_is_settled_after_the_grid(tmp_path):
    """Settlement runs at the start of the next same-ticker run, so the last
    date of each ticker would stay pending without an explicit pass."""
    run_backtest(["NVDA", "AAPL"], ["2026-01-05", "2026-01-12"], _config(tmp_path))
    assert sorted(_FakeGraph.instances[-1].settled) == ["AAPL", "NVDA"]


@pytest.mark.unit
def test_a_failed_cell_does_not_abort_the_sweep(tmp_path):
    _FakeGraph.fail_on = {("NVDA", "2026-01-05")}
    result = run_backtest(["NVDA"], ["2026-01-05", "2026-01-12"], _config(tmp_path))

    assert result.cells_run == 1
    assert result.failures == [("NVDA", "2026-01-05", "vendor exploded")]


# --- reading the result ------------------------------------------------------

def _log_with(tmp_path, rows):
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "m.md")})
    for ticker, date, decision, outcome in rows:
        log.store_decision(ticker, date, decision)
        if outcome is not None:
            log.update_with_outcome(ticker, date, outcome[0], outcome[1], 5, "note", "2026-02-01")
    return log


@pytest.mark.unit
def test_summary_scores_resolved_cells_and_keeps_pending_out_of_the_average(tmp_path):
    log = _log_with(tmp_path, [
        ("NVDA", "2026-01-05", "Rating: Buy\n\nx", (0.10, 0.04)),
        ("NVDA", "2026-01-12", "Rating: Buy\n\nx", (-0.02, -0.02)),
        ("AAPL", "2026-01-05", "Rating: Sell\n\nx", None),
    ])

    summary = summarize(log)

    assert summary.resolved == 2 and summary.pending == 1
    buys = summary.by_rating["Buy"]
    assert buys.count == 2 and buys.hit_rate == 0.5 and round(buys.mean_alpha, 4) == 0.01
    assert "Sell" not in summary.by_rating  # unsettled: nothing to score yet


@pytest.mark.unit
def test_summary_states_what_it_cannot_prove(tmp_path):
    text = summarize(_log_with(tmp_path, [("NVDA", "2026-01-05", DECISION, (0.1, 0.05))])).render()
    assert "not archived" in text
    assert "one" in text.lower() and "sampl" in text.lower()


@pytest.mark.unit
def test_the_analyst_set_under_test_is_the_one_that_runs(tmp_path):
    """A backtest of a two-analyst setup must not silently run four."""
    run_backtest(["NVDA"], ["2026-01-05"], _config(tmp_path), selected_analysts=["market", "news"])
    assert _FakeGraph.instances[-1].analysts == ["market", "news"]


@pytest.mark.unit
def test_a_run_id_cannot_escape_the_results_directory(tmp_path):
    """run_id becomes a path segment, so it is validated like a ticker is."""
    with pytest.raises(ValueError):
        run_backtest(["NVDA"], ["2026-01-05"], _config(tmp_path), run_id="../../escaped")
    with pytest.raises(ValueError):
        run_backtest(["NVDA"], ["2026-01-05"], _config(tmp_path), run_id="/etc/cron.d/x")


@pytest.mark.unit
def test_a_failed_settlement_does_not_lose_the_remaining_tickers(tmp_path, monkeypatch):
    """Settlement reflects with an LLM, so it can fail; the sweep still returns
    its result and every other ticker still gets settled."""
    settled = []

    def _settle(self, ticker):
        if ticker == "NVDA":
            raise RuntimeError("reflector timed out")
        settled.append(ticker)

    monkeypatch.setattr(_FakeGraph, "settle_pending", _settle, raising=False)
    result = run_backtest(["NVDA", "AAPL"], ["2026-01-05"], _config(tmp_path))

    assert result.cells_run == 2
    assert settled == ["AAPL"]
    assert result.settlement_failures == [("NVDA", "reflector timed out")]


@pytest.mark.unit
def test_pending_note_appears_only_when_something_is_pending(tmp_path):
    settled = [("NVDA", "2026-01-05", DECISION, (0.1, 0.05))]
    assert "Pending" not in summarize(_log_with(tmp_path, settled)).render()
    assert "Pending" in summarize(_log_with(tmp_path, settled + [("AAPL", "2026-01-05", DECISION, None)])).render()


# --- scoring reads the direction the rating claimed ---------------------------

def _scored(tmp_path, rows):
    log = _log_with(tmp_path, rows)
    return summarize(log).by_rating


@pytest.mark.unit
def test_a_bearish_call_that_fell_counts_as_right(tmp_path):
    """Alpha below the benchmark is the outcome a Sell predicted; scoring it as
    a miss reported the system as wrong exactly when it was right."""
    scores = _scored(tmp_path, [
        ("NVDA", "2026-01-05", "**Rating**: Sell\n\nx", (-0.08, -0.05)),
        ("AAPL", "2026-01-05", "**Rating**: Underweight\n\nx", (-0.03, -0.02)),
    ])
    assert scores["Sell"].hit_rate == 1.0
    assert scores["Underweight"].hit_rate == 1.0


@pytest.mark.unit
def test_a_bearish_call_that_rose_counts_as_wrong(tmp_path):
    scores = _scored(tmp_path, [("NVDA", "2026-01-05", "**Rating**: Sell\n\nx", (0.08, 0.05))])
    assert scores["Sell"].hit_rate == 0.0


@pytest.mark.unit
def test_a_bullish_call_is_scored_the_same_way_as_before(tmp_path):
    scores = _scored(tmp_path, [
        ("NVDA", "2026-01-05", "**Rating**: Buy\n\nx", (0.10, 0.04)),
        ("AAPL", "2026-01-05", "**Rating**: Buy\n\nx", (-0.02, -0.02)),
    ])
    assert scores["Buy"].hit_rate == 0.5


@pytest.mark.unit
def test_hold_claims_no_direction_so_it_gets_no_hit_rate(tmp_path):
    scores = _scored(tmp_path, [("NVDA", "2026-01-05", "**Rating**: Hold\n\nx", (0.01, 0.005))])
    assert scores["Hold"].hit_rate is None
    assert scores["Hold"].mean_alpha == 0.005


@pytest.mark.unit
def test_the_report_names_the_window_the_scores_cover(tmp_path):
    text = summarize(_log_with(tmp_path, [
        ("NVDA", "2026-01-05", "**Rating**: Buy\n\nx", (0.1, 0.05))])).render()
    assert "5" in text and "day" in text.lower()
    assert "Hold" not in text or "no direction" in text.lower()


@pytest.mark.unit
def test_the_window_reported_is_the_one_the_outcomes_used(tmp_path):
    """The log records the window each outcome was measured over; the summary
    must not claim a different one."""
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "m.md")})
    log.store_decision("NVDA", "2026-01-05", "**Rating**: Buy\n\nx")
    log.update_with_outcome("NVDA", "2026-01-05", 0.1, 0.04, 21, "note", "2026-02-01")

    assert "21 trading days" in summarize(log).render()


# --- mandate ------------------------------------------------------------------


@pytest.mark.unit
def test_the_mandate_under_test_is_the_one_that_runs(tmp_path):
    """A value sweep must not silently run unmandated: no analysts, 5-day grading."""
    run_backtest(["KO"], ["2026-01-05"], _config(tmp_path), mandate="equity_value")
    graph = _FakeGraph.instances[-1]
    assert graph.mandate_arg == "equity_value"
    entry = TradingMemoryLog({"memory_log_path": str(graph.config["memory_log_path"])}).load_entries()[0]
    assert entry["mandate"] == "equity_value"


@pytest.mark.unit
def test_no_mandate_argument_defers_to_the_config(tmp_path):
    """None keeps TRADINGAGENTS_MANDATE working exactly as it does for a single run."""
    run_backtest(["KO"], ["2026-01-05"], {**_config(tmp_path), "mandate": "equity_momentum"})
    graph = _FakeGraph.instances[-1]
    assert graph.mandate_arg is None
    assert graph.mandate_name == "equity_momentum"


@pytest.mark.unit
def test_resuming_under_the_same_mandate_skips_done_cells(tmp_path):
    cfg = _config(tmp_path)
    run_backtest(["KO"], ["2026-01-05"], cfg, run_id="r1", mandate="equity_value")
    again = run_backtest(["KO"], ["2026-01-05"], cfg, run_id="r1", mandate="equity_value")
    assert (again.cells_run, again.skipped) == (0, 1)


@pytest.mark.unit
def test_resuming_under_a_different_mandate_reruns_the_cells(tmp_path):
    """A value cell is not a momentum cell: different analysts, framing and horizon."""
    cfg = _config(tmp_path)
    run_backtest(["KO"], ["2026-01-05"], cfg, run_id="r1", mandate="equity_value")
    other = run_backtest(["KO"], ["2026-01-05"], cfg, run_id="r1", mandate="equity_momentum")
    assert (other.cells_run, other.skipped) == (1, 0)


# --- conviction: do stronger ratings mean bigger moves in their direction? ---------------


def _cells(tmp_path, spec):
    """spec: [(rating, [alpha, ...]), ...] -> a log of settled cells, one ticker each."""
    rows, n = [], 0
    for rating, alphas in spec:
        for alpha in alphas:
            rows.append((f"T{n:03d}", "2026-01-05", f"Rating: {rating}\n\nx", (alpha, alpha)))
            n += 1
    return _log_with(tmp_path, rows)


@pytest.mark.unit
def test_ratings_as_position_tilts(tmp_path):
    """Buy +1 x +10%, Sell -1 x -10%, Overweight +1/2 x +4% -> mean (0.10+0.10+0.02)/3."""
    c = summarize(_cells(tmp_path, [("Buy", [0.10]), ("Sell", [-0.10]), ("Overweight", [0.04])])).conviction
    assert c.tilted_alpha == pytest.approx(0.22 / 3, abs=1e-6)
    assert c.cells == 3


@pytest.mark.unit
def test_stronger_calls_doing_better_ranks_positive_and_in_order(tmp_path):
    c = summarize(_cells(tmp_path, [
        ("Buy", [0.08, 0.09, 0.07, 0.10, 0.06]),
        ("Overweight", [0.02, 0.03, 0.01, 0.02, 0.03]),
        ("Sell", [-0.05, -0.06, -0.04, -0.07, -0.05]),
    ])).conviction
    assert c.rank_correlation > 0.8
    assert c.inversions == []
    text = "\n".join(c.render())
    assert "In order: Buy > Overweight > Sell by mean alpha." in text


@pytest.mark.unit
def test_a_weaker_rating_beating_a_stronger_one_is_named(tmp_path):
    c = summarize(_cells(tmp_path, [
        ("Buy", [0.01, 0.02, 0.01, 0.02, 0.02]),
        ("Overweight", [0.08, 0.07, 0.09, 0.08, 0.07]),
    ])).conviction
    assert c.inversions == [("Overweight", "Buy")]
    assert "Out of order: Overweight beat Buy" in "\n".join(c.render())


@pytest.mark.unit
def test_a_thin_rating_is_named_and_not_ranked(tmp_path):
    """Two bad Buys are two stocks' luck, not evidence that Buy underperforms."""
    c = summarize(_cells(tmp_path, [
        ("Buy", [-0.05, -0.04]),
        ("Overweight", [0.03, 0.02, 0.04, 0.03, 0.02]),
    ])).conviction
    assert c.inversions == []
    assert c.thin == [("Buy", 2)]
    text = "\n".join(c.render())
    assert "Too few settled cells to rank: Buy (n=2)" in text
    assert "In order" not in text, "one rankable rating is not an ordering"


@pytest.mark.unit
def test_rank_correlation_needs_varied_ratings(tmp_path):
    c = summarize(_cells(tmp_path, [("Buy", [0.01, 0.02, 0.03, 0.04, 0.05])])).conviction
    assert c.rank_correlation is None
    assert "n/a" in "\n".join(c.render())


@pytest.mark.unit
def test_nothing_settled_prints_no_conviction_section(tmp_path):
    log = _log_with(tmp_path, [("NVDA", "2026-01-05", "Rating: Buy\n\nx", None)])
    assert "Conviction" not in summarize(log).render()
