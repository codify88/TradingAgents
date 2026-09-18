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
    g.config = {}
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


def test_run_signature_keys_on_the_mandates_own_analysts():
    """Mandate analysts change the graph; a pre-analyst checkpoint must not resume."""
    def sig(name):
        g = _graph(name)
        g.selected_analysts = ("market",)
        g.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
        return g._run_signature("stock")

    assert sig("equity_value").endswith("|mandate_analysts=quality,valuation")
    # Mandates without analysts, and no mandate, keep their existing signature.
    # (upstream v0.5.0 added the portfolio field ahead of ours.)
    assert sig("equity_momentum") == (
        "analysts=market|debate=1|risk=1|asset=stock|mandate=equity_momentum"
        "|portfolio=none"
    )
    assert "mandate_analysts" not in sig("")


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
        resolution_date="2028-01-05", mandate="equity_value",
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
        "mandate": "equity_momentum",
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


# --- config / CLI wiring --------------------------------------------------


def test_graph_takes_the_mandate_from_config_when_not_passed():
    """Unattended runs configure the mandate via TRADINGAGENTS_MANDATE."""
    g = object.__new__(TradingAgentsGraph)
    g.config = {"mandate": "equity_momentum"}
    mandate = None
    resolved = (mandate if mandate is not None else g.config.get("mandate", "")) or ""
    assert get_mandate(resolved) is EQUITY_MOMENTUM


def test_config_exposes_a_mandate_key_defaulting_to_none():
    from tradingagents.default_config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["mandate"] == ""


def test_mandate_env_var_is_registered_for_override():
    from tradingagents.default_config import _ENV_OVERRIDES

    assert _ENV_OVERRIDES["TRADINGAGENTS_MANDATE"] == "mandate"


def test_cli_picker_offers_every_registered_mandate_plus_none(monkeypatch):
    """The picker reads the registry, so it can't drift from what's registered."""
    import cli.utils as u

    captured = {}

    class _Q:
        def __init__(self, choices):
            captured["values"] = [c.value for c in choices]

        def ask(self):
            return ""

    monkeypatch.setattr(
        u.questionary, "select", lambda *a, **kw: _Q(kw["choices"])
    )
    assert u.select_mandate() == ""
    assert captured["values"] == [m.name for m in list_mandates()] + [""]


def test_tuple_system_message_is_reported_clearly():
    """Upstream's fundamentals analyst shipped a 1-tuple here (stray comma), which
    the template rendered as Python tuple syntax into the model's prompt."""
    from tradingagents.agents.utils.agent_utils import apply_mandate_to_system_message

    with pytest.raises(TypeError, match="trailing comma"):
        apply_mandate_to_system_message({}, "fundamentals", ("oops",))


def test_every_analyst_builds_a_string_system_message():
    """Guards the whole class of bug, not just the one instance."""
    import ast
    import pathlib

    for path in sorted(pathlib.Path("tradingagents/agents/analysts").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Assign)
                and getattr(node.targets[0], "id", "") == "system_message"
            ):
                assert not isinstance(node.value, ast.Tuple), (
                    f"{path.name}: system_message is a tuple -- stray trailing comma"
                )


def test_same_ticker_and_date_under_two_mandates_are_two_entries(tmp_path):
    """Found by running KO through both mandates on one date: the dedupe key was
    (date, ticker), so the second run was silently swallowed. Two mandates are
    two decisions -- different horizon, different framing, different outcome."""
    log = _log(tmp_path)
    log.store_decision("KO", "2026-09-17", "**Rating**: Hold", mandate="equity_value")
    log.store_decision("KO", "2026-09-17", "**Rating**: Hold", mandate="equity_momentum")

    entries = log.load_entries()
    assert [e["mandate"] for e in entries] == ["equity_value", "equity_momentum"]

    # Still idempotent within a mandate.
    log.store_decision("KO", "2026-09-17", "**Rating**: Hold", mandate="equity_value")
    assert len(log.load_entries()) == 2


def test_each_mandates_entry_resolves_on_its_own_clock(tmp_path):
    """The momentum call settles in months; the value call is still pending."""
    log = _log(tmp_path)
    for name in ("equity_value", "equity_momentum"):
        log.store_decision("KO", "2026-09-17", "**Rating**: Hold", mandate=name)

    log.batch_update_with_outcomes([{
        "ticker": "KO", "trade_date": "2026-09-17", "mandate": "equity_momentum",
        "raw_return": 0.03, "alpha_return": -0.01,
        "holding_days": EQUITY_MOMENTUM.horizon_days,
        "reflection": "Trend held, lagged the market.",
        "resolution_date": "2027-03-19",
    }])

    by_mandate = {e["mandate"]: e for e in log.load_entries()}
    assert by_mandate["equity_momentum"]["pending"] is False
    assert by_mandate["equity_momentum"]["holding"] == f"{EQUITY_MOMENTUM.horizon_days}d"
    assert by_mandate["equity_value"]["pending"] is True


def test_resolution_targets_the_named_mandate_only(tmp_path):
    """update_with_outcome must not settle whichever entry it happens to hit first."""
    log = _log(tmp_path)
    for name in ("equity_value", "equity_momentum"):
        log.store_decision("KO", "2026-09-17", "**Rating**: Hold", mandate=name)

    log.update_with_outcome(
        ticker="KO", trade_date="2026-09-17", raw_return=0.03, alpha_return=-0.01,
        holding_days=EQUITY_MOMENTUM.horizon_days, reflection="r",
        resolution_date="2027-03-19", mandate="equity_momentum",
    )
    by_mandate = {e["mandate"]: e for e in log.load_entries()}
    assert by_mandate["equity_value"]["pending"] is True
    assert by_mandate["equity_momentum"]["pending"] is False


# --- indicator shortlist ----------------------------------------------------


def test_market_analyst_is_told_the_mandates_indicator_shortlist():
    guidance = EQUITY_VALUE.guidance_for("market")
    assert "close_200_sma, close_50_sma, atr" in guidance
    assert "do not call get_indicators for any other" in guidance
    # The analyst-specific framing is kept, not replaced.
    assert "Price action is secondary" in guidance


def test_shortlist_reaches_only_the_market_analyst():
    assert "Indicator selection" not in EQUITY_VALUE.guidance_for("fundamentals")
    assert "Indicator selection" not in EQUITY_MOMENTUM.guidance_for("news")


def test_shortlist_alone_still_produces_market_guidance():
    m = Mandate(name="x", label="X", description="d", indicator_shortlist=("rsi",))
    assert m.guidance_for("market").startswith("Indicator selection")


def test_no_shortlist_leaves_market_guidance_unchanged():
    m = Mandate(name="x", label="X", description="d",
                analyst_guidance={"market": "Just this."})
    assert m.guidance_for("market") == "Just this."


def test_unknown_indicator_in_shortlist_is_rejected_at_construction():
    with pytest.raises(ValueError, match="unknown indicator.*stochrsi"):
        Mandate(name="x", label="X", description="d",
                indicator_shortlist=("rsi", "stochrsi"))


def test_known_indicator_set_matches_the_market_analysts_menu():
    """Drift guard: the shortlist vocabulary is the menu upstream's prompt offers."""
    import inspect
    import re

    from tradingagents.agents.analysts import market_analyst
    from tradingagents.mandates.base import MARKET_ANALYST_INDICATORS

    menu = set(re.findall(r"^- (\w+):", inspect.getsource(market_analyst), re.M))
    assert menu == MARKET_ANALYST_INDICATORS


def test_shortlist_reaches_the_rendered_market_prompt():
    from tradingagents.agents.utils.agent_utils import apply_mandate_to_system_message

    state = {"mandate": "equity_value", "mandate_context": render_mandate_context(EQUITY_VALUE)}
    rendered = apply_mandate_to_system_message(state, "market", "UPSTREAM PROMPT")
    assert rendered.index("UPSTREAM PROMPT") < rendered.index("Indicator selection")
