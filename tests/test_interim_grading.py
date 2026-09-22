"""Interim grading: a long-horizon decision is checkpointed before it settles.

Upstream settles a pending entry exactly once, at a single horizon. A mandate
declares ``review_horizons_days`` as well, so an equity_value call graded at 504
days still produces a usable lesson at 63, 126 and 252 days instead of teaching
nothing for two years. These tests cover the log schema that holds several
outcomes per entry, the point-in-time gate on those outcomes, and the graph loop
that decides between checkpointing and settling.
"""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.graph.reflection import Reflector
from tradingagents.graph.trading_graph import TradingAgentsGraph

_SEP = TradingMemoryLog._SEPARATOR

DECISION = "Rating: Hold\nMaintain KO at current weight; 2-year horizon, $77 stop."


def make_log(tmp_path):
    return TradingMemoryLog({"memory_log_path": str(tmp_path / "trading_memory.md")})


def review(days, resolved, raw, alpha, note, ticker="KO", date="2026-09-17",
           mandate="equity_value"):
    return {
        "ticker": ticker,
        "trade_date": date,
        "mandate": mandate,
        "horizon_days": days,
        "raw_return": raw,
        "alpha_return": alpha,
        "resolution_date": resolved,
        "note": note,
    }


def _price_df(prices, start="2026-09-17"):
    idx = pd.date_range(start=start, periods=len(prices), freq="D")
    return pd.DataFrame({"Close": prices}, index=idx)


def _graph_mock(mandate_name="equity_value", horizons=(63, 126, 252, 504), primary=504):
    g = MagicMock(spec=TradingAgentsGraph)
    g.mandate_name = mandate_name
    g.reflector = MagicMock()          # instance attribute: not on the spec
    g._holding_days_for.return_value = primary
    g._horizons_for.return_value = horizons
    g._resolve_benchmark.return_value = "SPY"
    # Keep the real checkpoint logic; each test pins the price lookup itself.
    g._due_reviews = lambda *a, **k: TradingAgentsGraph._due_reviews(g, *a, **k)
    return g


# ---------------------------------------------------------------------------
# Log schema: several outcomes per entry
# ---------------------------------------------------------------------------

class TestReviewSchema:

    def test_review_is_appended_and_entry_stays_pending(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        log.batch_append_reviews([
            review(63, "2026-12-16", 0.032, 0.011, "Tracking; multiple still full.")
        ])
        entry = log.load_entries()[0]
        assert entry["pending"] is True, "a checkpoint must not settle the entry"
        assert len(entry["reviews"]) == 1
        r = entry["reviews"][0]
        assert (r["days"], r["resolved"], r["raw"], r["alpha"]) == (
            63, "2026-12-16", "+3.2%", "+1.1%"
        )
        assert r["note"] == "Tracking; multiple still full."

    def test_review_does_not_pollute_the_decision_text(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        log.batch_append_reviews([review(63, "2026-12-16", 0.032, 0.011, "Tracking.")])
        assert log.load_entries()[0]["decision"] == DECISION.strip()

    def test_several_horizons_land_in_order(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        log.batch_append_reviews([
            review(126, "2027-03-17", 0.08, 0.02, "Second."),
            review(63, "2026-12-16", 0.03, 0.01, "First."),
        ])
        assert [r["days"] for r in log.load_entries()[0]["reviews"]] == [63, 126]

    def test_same_horizon_is_never_written_twice(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        for _ in range(2):
            log.batch_append_reviews([review(63, "2026-12-16", 0.03, 0.01, "Tracking.")])
        assert len(log.load_entries()[0]["reviews"]) == 1

    def test_reviews_survive_final_settlement(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        log.batch_append_reviews([review(63, "2026-12-16", 0.03, 0.01, "Tracking.")])
        log.update_with_outcome(
            "KO", "2026-09-17", 0.21, 0.04, 504, "Thesis paid off slowly.",
            resolution_date="2028-09-15", mandate="equity_value",
        )
        entry = log.load_entries()[0]
        assert entry["pending"] is False
        assert entry["reflection"] == "Thesis paid off slowly."
        assert entry["decision"] == DECISION.strip()
        assert [r["days"] for r in entry["reviews"]] == [63], "history must be kept"
        assert entry["resolved"] == "2028-09-15"

    def test_reviews_are_keyed_by_mandate(self, tmp_path):
        """Same ticker and date under two mandates: a review must not cross over."""
        log = make_log(tmp_path)
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_momentum")
        log.batch_append_reviews([
            review(21, "2026-10-15", 0.01, 0.00, "Momentum note.", mandate="equity_momentum"),
        ])
        by_mandate = {e["mandate"]: e for e in log.load_entries()}
        assert by_mandate["equity_momentum"]["reviews"][0]["note"] == "Momentum note."
        assert by_mandate["equity_value"]["reviews"] == []

    def test_noop_without_log_file(self, tmp_path):
        log = TradingMemoryLog({})
        log.batch_append_reviews([review(63, "2026-12-16", 0.03, 0.01, "x")])  # no raise

    def test_rotation_never_prunes_a_mandated_pending_entry(self, tmp_path):
        """A pending tag ending in ``mandate:...]`` is still pending.

        Rotation used to suffix-match ``| pending]``, so the mandate marker made
        unresolved long-horizon work look resolved and prunable.
        """
        log = TradingMemoryLog({
            "memory_log_path": str(tmp_path / "trading_memory.md"),
            "memory_log_max_entries": 1,
        })
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        for i in (1, 2):
            log.store_decision("NVDA", f"2026-01-0{i}", "Rating: Buy")
            log.update_with_outcome("NVDA", f"2026-01-0{i}", 0.05, 0.02, 5, f"Lesson {i}.")
        pending = log.get_pending_entries()
        assert [e["mandate"] for e in pending] == ["equity_value"]


# ---------------------------------------------------------------------------
# Injection: an open thesis teaches, without pretending to be a verdict
# ---------------------------------------------------------------------------

class TestReviewInjection:

    def test_pending_entry_with_a_due_review_is_injected(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        log.batch_append_reviews([
            review(63, "2026-12-16", 0.032, 0.011, "Tracking; margin holding.")
        ])
        ctx = log.get_past_context("KO")
        assert "Past analyses of KO" in ctx
        assert "Tracking; margin holding." in ctx
        assert "INTERIM REVIEW at 63d" in ctx

    def test_injected_review_is_labelled_in_progress_not_resolved(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        log.batch_append_reviews([review(63, "2026-12-16", 0.032, 0.011, "Tracking.")])
        ctx = log.get_past_context("KO")
        assert "in progress" in ctx, "a checkpoint must not read as a settled outcome"
        assert "REFLECTION:" not in ctx

    def test_pending_without_any_review_is_still_excluded(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        assert log.get_past_context("KO") == ""

    def test_review_after_as_of_is_not_visible(self, tmp_path):
        """#1251 holds for checkpoints: a backtest cannot read a future review."""
        log = make_log(tmp_path)
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        log.batch_append_reviews([review(63, "2026-12-16", 0.032, 0.011, "Tracking.")])
        assert log.get_past_context("KO", as_of="2026-11-01") == ""
        assert "Tracking." in log.get_past_context("KO", as_of="2026-12-16")

    def test_only_due_reviews_are_shown(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        log.batch_append_reviews([
            review(63, "2026-12-16", 0.03, 0.01, "First checkpoint."),
            review(126, "2027-03-17", 0.08, 0.02, "Second checkpoint."),
        ])
        ctx = log.get_past_context("KO", as_of="2027-01-01")
        assert "First checkpoint." in ctx
        assert "Second checkpoint." not in ctx

    def test_cross_ticker_uses_the_latest_review_note(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        log.batch_append_reviews([
            review(63, "2026-12-16", 0.03, 0.01, "First checkpoint."),
            review(126, "2027-03-17", 0.08, 0.02, "Second checkpoint."),
        ])
        ctx = log.get_past_context("NVDA")
        assert "Recent cross-ticker lessons" in ctx
        assert "Second checkpoint." in ctx
        assert "First checkpoint." not in ctx


# ---------------------------------------------------------------------------
# Price window: several horizons from one download
# ---------------------------------------------------------------------------

class TestReturnsAtHorizons:

    def _patched(self, stock, spy, horizons, ticker="KO"):
        with patch("yfinance.Ticker") as cls:
            cls.side_effect = lambda sym: MagicMock(
                **{"history.return_value": _price_df(spy if sym == "SPY" else stock)}
            )
            return TradingAgentsGraph._returns_at_horizons(
                ticker, "2026-09-17", horizons, "SPY",
            )

    def test_settles_only_horizons_that_have_fully_traded(self):
        settled = self._patched(
            [100.0] + [104.0] * 9, [100.0] * 10, (5, 20, 60),
        )
        assert set(settled) == {5}, "#1169: an untraded window must stay unsettled"

    def test_alpha_is_measured_against_the_benchmark(self):
        settled = self._patched(
            [100.0, 101.0, 102.0, 103.0, 104.0, 110.0],
            [100.0, 100.0, 100.0, 100.0, 100.0, 105.0],
            (5,),
        )
        raw, alpha, resolved = settled[5]
        assert raw == pytest.approx(0.10)
        assert alpha == pytest.approx(0.05)
        assert resolved == "2026-09-22"

    def test_one_download_serves_every_horizon(self):
        calls = []
        with patch("yfinance.Ticker") as cls:
            def _make(sym):
                calls.append(sym)
                return MagicMock(**{"history.return_value": _price_df([100.0 + i for i in range(30)])})
            cls.side_effect = _make
            settled = TradingAgentsGraph._returns_at_horizons(
                "KO", "2026-09-17", (5, 10, 20), "SPY",
            )
        assert set(settled) == {5, 10, 20}
        assert len(calls) == 2, "one request per symbol regardless of horizon count"

    def test_unreachable_symbol_settles_nothing(self):
        with patch("yfinance.Ticker", side_effect=RuntimeError("delisted")):
            assert TradingAgentsGraph._returns_at_horizons(
                "XXXXFAKE", "2026-09-17", (5,), "SPY",
            ) == {}

    def test_empty_horizons_makes_no_request(self):
        with patch("yfinance.Ticker") as cls:
            assert TradingAgentsGraph._returns_at_horizons("KO", "2026-09-17", (), "SPY") == {}
            cls.assert_not_called()

    def test_calendar_span_covers_a_two_year_horizon(self):
        """A fixed weekend buffer never settles a 504-trading-day window.

        504 sessions span ~730 calendar days; the old ``holding_days + 7`` asked
        for 511 and the entry would have stayed pending forever.
        """
        assert TradingAgentsGraph._calendar_span(504) >= 730
        assert TradingAgentsGraph._calendar_span(252) >= 365
        assert TradingAgentsGraph._calendar_span(5) >= 7


# ---------------------------------------------------------------------------
# The loop: checkpoint while open, settle once at the primary horizon
# ---------------------------------------------------------------------------

class TestResolveLoop:

    def test_interim_review_written_when_primary_has_not_settled(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        g = _graph_mock()
        g.memory_log = log
        g._fetch_returns = MagicMock(return_value=(None, None, None, None))
        g.reflector.reflect_on_interim_outcome.return_value = "Tracking, early days."
        g._returns_at_horizons = MagicMock(return_value={63: (0.03, 0.01, "2026-12-16")})
        TradingAgentsGraph._resolve_pending_entries(g, "KO")
        entry = log.load_entries()[0]
        assert entry["pending"] is True
        assert [r["days"] for r in entry["reviews"]] == [63]
        assert entry["reviews"][0]["note"] == "Tracking, early days."
        g.reflector.reflect_on_final_decision.assert_not_called()

    def test_interim_reflection_is_told_how_much_horizon_elapsed(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        g = _graph_mock()
        g.memory_log = log
        g._fetch_returns = MagicMock(return_value=(None, None, None, None))
        g._returns_at_horizons = MagicMock(return_value={63: (0.03, 0.01, "2026-12-16")})
        TradingAgentsGraph._resolve_pending_entries(g, "KO")
        kwargs = g.reflector.reflect_on_interim_outcome.call_args.kwargs
        assert kwargs["elapsed_days"] == 63
        assert kwargs["horizon_days"] == 504

    def test_settling_skips_superseded_checkpoints(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        g = _graph_mock()
        g.memory_log = log
        g._fetch_returns = MagicMock(return_value=(0.21, 0.04, 504, "2028-09-15"))
        g.reflector.reflect_on_final_decision.return_value = "Paid off."
        TradingAgentsGraph._resolve_pending_entries(g, "KO")
        entry = log.load_entries()[0]
        assert entry["pending"] is False
        assert entry["reflection"] == "Paid off."
        assert entry["reviews"] == []
        g.reflector.reflect_on_interim_outcome.assert_not_called()

    def test_recorded_horizon_is_not_reviewed_again(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("KO", "2026-09-17", DECISION, mandate="equity_value")
        log.batch_append_reviews([review(63, "2026-12-16", 0.03, 0.01, "Already noted.")])
        g = _graph_mock()
        g.memory_log = log
        g._fetch_returns = MagicMock(return_value=(None, None, None, None))
        g._returns_at_horizons = MagicMock(return_value={})
        TradingAgentsGraph._resolve_pending_entries(g, "KO")
        requested = g._returns_at_horizons.call_args[0][2]
        assert 63 not in requested, "a recorded checkpoint must not be re-priced"
        assert list(requested) == [126, 252]
        assert len(log.load_entries()[0]["reviews"]) == 1

    def test_no_mandate_makes_no_interim_request(self, tmp_path):
        """An unmandated run keeps upstream's exact code path — one horizon."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-05", "Rating: Buy")
        g = _graph_mock(mandate_name="", horizons=(5,), primary=5)
        g.memory_log = log
        g._fetch_returns = MagicMock(return_value=(None, None, None, None))
        g._returns_at_horizons = MagicMock()
        TradingAgentsGraph._resolve_pending_entries(g, "NVDA")
        g._returns_at_horizons.assert_not_called()
        assert len(log.get_pending_entries()) == 1


# ---------------------------------------------------------------------------
# The interim prompt must not manufacture a verdict
# ---------------------------------------------------------------------------

class TestInterimReflectionPrompt:

    def _invoke(self, **kwargs):
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content="Tracking.")
        out = Reflector(llm).reflect_on_interim_outcome(
            final_decision="Rating: Hold", raw_return=0.03, alpha_return=0.01,
            elapsed_days=63, horizon_days=504, **kwargs,
        )
        return out, llm.invoke.call_args[0][0]

    def test_returns_llm_output(self):
        out, _ = self._invoke()
        assert out == "Tracking."

    def test_prompt_states_the_elapsed_fraction_and_figures(self):
        _, messages = self._invoke()
        human = messages[1][1]
        assert "63 of 504 trading days" in human
        assert "12% of the horizon" in human
        assert "+3.0%" in human and "+1.0%" in human

    def test_prompt_forbids_calling_the_outcome(self):
        _, messages = self._invoke()
        system = messages[0][1]
        assert "not yet known" in system
        assert "must not declare the call right or wrong" in system

    def test_benchmark_label_is_threaded(self):
        _, messages = self._invoke(benchmark_name="MTUM")
        assert "Alpha vs MTUM" in messages[1][1]
