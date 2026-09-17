"""Coverage for the mandate abstraction (docs/design/mandates.md)."""

from unittest.mock import MagicMock

import pytest

from tradingagents.agents.utils.agent_utils import (
    get_mandate_context_from_state,
    mandate_section,
)
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.mandates import (
    EQUITY_MOMENTUM,
    EQUITY_VALUE,
    Mandate,
    get_mandate,
    list_mandates,
    render_mandate_context,
)

# --- registry -------------------------------------------------------------


def test_unknown_mandate_raises_rather_than_falling_back():
    """A typo must fail loudly, not silently run a different investment style."""
    with pytest.raises(ValueError, match="Unknown mandate"):
        get_mandate("equity_vaule")


@pytest.mark.parametrize("empty", [None, ""])
def test_absent_mandate_resolves_to_none(empty):
    assert get_mandate(empty) is None


def test_registered_mandates_are_discoverable():
    assert {m.name for m in list_mandates()} == {"equity_value", "equity_momentum"}


# --- horizons -------------------------------------------------------------


def test_long_horizon_mandates_do_not_inherit_the_five_day_default():
    """The whole point: a value thesis is not graded on a swing-trade clock."""
    assert EQUITY_VALUE.horizon_days > TradingAgentsGraph.DEFAULT_HOLDING_DAYS
    assert EQUITY_MOMENTUM.horizon_days > TradingAgentsGraph.DEFAULT_HOLDING_DAYS
    assert EQUITY_VALUE.horizon_days > EQUITY_MOMENTUM.horizon_days


def test_all_horizons_ends_at_the_primary_horizon():
    assert EQUITY_VALUE.all_horizons_days[-1] == EQUITY_VALUE.horizon_days
    assert list(EQUITY_VALUE.all_horizons_days) == sorted(EQUITY_VALUE.all_horizons_days)


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"horizon_days": 0}, "horizon_days must be >= 1"),
        ({"horizon_days": 100, "review_horizons_days": (100,)}, "must be in"),
        ({"horizon_days": 100, "review_horizons_days": (150,)}, "must be in"),
        ({"horizon_days": 100, "review_horizons_days": (60, 30)}, "ascending"),
    ],
)
def test_invalid_horizons_are_rejected_at_construction(kwargs, match):
    with pytest.raises(ValueError, match=match):
        Mandate(name="x", label="X", description="d", **kwargs)


# --- prompt rendering -----------------------------------------------------


def test_no_mandate_renders_empty_so_prompts_match_upstream():
    assert render_mandate_context(None) == ""
    assert get_mandate_context_from_state({}) == ""
    assert mandate_section({}) == ""


def test_rendered_context_carries_horizon_and_screens():
    text = render_mandate_context(EQUITY_VALUE)
    assert EQUITY_VALUE.label in text
    assert str(EQUITY_VALUE.horizon_days) in text
    assert "margin of safety" in text.lower()


def test_state_falls_back_to_rendering_from_the_wire_name():
    """A state carrying only the name still yields the full block."""
    assert "Value" in get_mandate_context_from_state({"mandate": "equity_value"})


def test_unknown_name_mid_graph_degrades_instead_of_killing_the_run():
    assert get_mandate_context_from_state({"mandate": "nope"}) == ""


def test_mandate_section_pads_only_when_present():
    assert mandate_section({"mandate": "equity_value"}).endswith("\n\n")


# --- state plumbing -------------------------------------------------------


def test_initial_state_carries_the_mandate():
    state = Propagator().create_initial_state(
        "AAPL", "2026-09-17", mandate="equity_value", mandate_context="CTX"
    )
    assert state["mandate"] == "equity_value"
    assert state["mandate_context"] == "CTX"


def test_initial_state_without_a_mandate_is_unchanged_from_upstream():
    state = Propagator().create_initial_state("AAPL", "2026-09-17")
    assert state["mandate"] == ""
    assert state["mandate_context"] == ""


# --- grading horizon ------------------------------------------------------


def _graph(mandate_name=""):
    g = object.__new__(TradingAgentsGraph)
    g.mandate_name = mandate_name
    g.mandate = get_mandate(mandate_name)
    return g


def test_holding_days_defaults_to_upstream_without_a_mandate():
    assert _graph()._holding_days_for() == TradingAgentsGraph.DEFAULT_HOLDING_DAYS


def test_holding_days_follows_the_active_mandate():
    assert _graph("equity_value")._holding_days_for() == EQUITY_VALUE.horizon_days


def test_entrys_own_mandate_wins_over_the_current_run():
    """A value call logged last year must not be graded on a momentum clock."""
    g = _graph("equity_momentum")
    assert g._holding_days_for("equity_value") == EQUITY_VALUE.horizon_days


def test_horizon_from_a_mandate_this_build_no_longer_knows_falls_back():
    assert _graph()._holding_days_for("retired_style") == TradingAgentsGraph.DEFAULT_HOLDING_DAYS


# --- benchmark precedence -------------------------------------------------


def _benchmark_graph(mandate_benchmark, explicit=None):
    g = MagicMock(spec=TradingAgentsGraph)
    g.config = {"benchmark_ticker": explicit, "benchmark_map": {"": "SPY", ".T": "^N225"}}
    g.mandate = (
        None if mandate_benchmark is None
        else Mandate(name="m", label="M", description="d", benchmark=mandate_benchmark)
    )
    return g


def test_mandate_benchmark_overrides_the_exchange_map():
    g = _benchmark_graph("MTUM")
    assert TradingAgentsGraph._resolve_benchmark(g, "NVDA") == "MTUM"


def test_explicit_config_benchmark_still_beats_the_mandate():
    g = _benchmark_graph("MTUM", explicit="QQQ")
    assert TradingAgentsGraph._resolve_benchmark(g, "NVDA") == "QQQ"


def test_mandate_without_a_benchmark_keeps_per_exchange_defaults():
    g = _benchmark_graph(None)
    assert TradingAgentsGraph._resolve_benchmark(g, "7203.T") == "^N225"


# --- checkpoint signature -------------------------------------------------


def test_run_signature_separates_mandates():
    """Resuming under a different mandate must start fresh, not continue."""
    def sig(name):
        g = _graph(name)
        g.selected_analysts = ("market",)
        g.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
        return g._run_signature("stock")

    assert sig("equity_value") != sig("equity_momentum") != sig("")


# --- memory log round-trip ------------------------------------------------


def _log(tmp_path):
    return TradingMemoryLog({"memory_log_path": str(tmp_path / "log.md")})


def test_decision_records_its_mandate(tmp_path):
    log = _log(tmp_path)
    log.store_decision("AAPL", "2026-01-05", "**Rating**: Buy", mandate="equity_value")
    entry = log.load_entries()[0]
    assert entry["mandate"] == "equity_value"
    assert entry["pending"] is True


def test_pending_entry_with_a_mandate_is_still_idempotent(tmp_path):
    """The dedupe scan once matched on a '| pending]' suffix the tag no longer has."""
    log = _log(tmp_path)
    for _ in range(2):
        log.store_decision("AAPL", "2026-01-05", "**Rating**: Buy", mandate="equity_value")
    assert len(log.load_entries()) == 1


def test_mandate_survives_outcome_resolution(tmp_path):
    log = _log(tmp_path)
    log.store_decision("AAPL", "2026-01-05", "**Rating**: Buy", mandate="equity_value")
    log.update_with_outcome(
        ticker="AAPL", trade_date="2026-01-05", raw_return=0.12, alpha_return=0.04,
        holding_days=EQUITY_VALUE.horizon_days, reflection="Held up.",
        resolution_date="2028-01-05",
    )
    entry = log.load_entries()[0]
    assert entry["pending"] is False
    assert entry["mandate"] == "equity_value"
    assert entry["resolved"] == "2028-01-05"
    assert entry["alpha"] == "+4.0%"
    assert entry["holding"] == f"{EQUITY_VALUE.horizon_days}d"


def test_batch_resolution_preserves_the_mandate(tmp_path):
    log = _log(tmp_path)
    log.store_decision("NVDA", "2026-01-05", "**Rating**: Buy", mandate="equity_momentum")
    log.batch_update_with_outcomes([{
        "ticker": "NVDA", "trade_date": "2026-01-05", "raw_return": -0.05,
        "alpha_return": -0.08, "holding_days": EQUITY_MOMENTUM.horizon_days,
        "reflection": "Trend broke.", "resolution_date": "2026-07-05",
    }])
    entry = log.load_entries()[0]
    assert entry["mandate"] == "equity_momentum"
    assert entry["raw"] == "-5.0%"


def test_entries_written_before_mandates_still_parse(tmp_path):
    """Back-compat: a legacy 7-field tag must keep its alpha/holding/resolved."""
    path = tmp_path / "log.md"
    path.write_text(
        "[2026-01-05 | AAPL | Buy | +2.0% | +0.5% | 5d | resolved:2026-01-12]\n\n"
        "DECISION:\nold\n\nREFLECTION:\nlesson\n",
        encoding="utf-8",
    )
    entry = TradingMemoryLog({"memory_log_path": str(path)}).load_entries()[0]
    assert entry["mandate"] == ""
    assert (entry["raw"], entry["alpha"], entry["holding"]) == ("+2.0%", "+0.5%", "5d")
    assert entry["resolved"] == "2026-01-12"
