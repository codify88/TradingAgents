"""Mandate analysts: declaration, graph wiring, prompt reach, reports, CLI.

The extension point P2 pays for once so P3 can be purely additive: a mandate
declares analysts, and they run, report, reach every downstream agent, and
show up in the saved report tree -- with no per-analyst upstream edit.
"""

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode

from tradingagents.agents.utils.agent_states import merge_mandate_reports
from tradingagents.agents.utils.agent_utils import mandate_section
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.setup import GraphSetup
from tradingagents.mandates import EQUITY_MOMENTUM, EQUITY_VALUE, Mandate, render_mandate_context
from tradingagents.mandates.analysts import MandateAnalyst, create_mandate_analyst
from tradingagents.mandates.base import RESERVED_ANALYST_KEYS, RESERVED_NODE_NAMES
from tradingagents.mandates.graph import iter_mandate_reports, render_mandate_reports


@tool
def dummy_tool(ticker: str) -> str:
    """A tool."""
    return ticker


def analyst(key="alpha", label="Alpha Analyst", tools=(dummy_tool,), msg="Do alpha."):
    return MandateAnalyst(key=key, label=label, tools=tools, system_message=msg)


# --- declaration --------------------------------------------------------------


class TestDeclaration:

    @pytest.mark.parametrize("kwargs, match", [
        ({"key": "not an id"}, "identifier"),
        ({"label": "Alpha"}, "must end in ' Analyst'"),
        ({"tools": ()}, "at least one tool"),
    ])
    def test_invalid_analyst_rejected(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            analyst(**kwargs)

    def test_node_names_derive_from_the_label_and_key(self):
        a = analyst()
        assert (a.clear_node, a.tool_node) == ("Msg Clear Alpha", "tools_alpha")

    @pytest.mark.parametrize("bad, match", [
        (analyst(key="market"), "upstream's"),
        (analyst(key="alpha", label="Market Analyst"), "upstream node"),
        (analyst(label="Neutral Analyst"), "upstream node"),
    ])
    def test_mandate_rejects_collisions_with_upstream(self, bad, match):
        with pytest.raises(ValueError, match=match):
            Mandate(name="x", label="X", description="d", analysts=(bad,))

    def test_mandate_rejects_duplicate_keys(self):
        with pytest.raises(ValueError, match="duplicate"):
            Mandate(name="x", label="X", description="d",
                    analysts=(analyst(), analyst(label="Beta Analyst")))

    def test_reserved_keys_track_upstream(self):
        """Drift guard: a new upstream analyst must become reserved here too."""
        assert set(ANALYST_NODE_SPECS) == RESERVED_ANALYST_KEYS
        assert {s.agent_node for s in ANALYST_NODE_SPECS.values()} <= RESERVED_NODE_NAMES

    def test_reserved_nodes_cover_every_fixed_upstream_node(self):
        g = GraphSetup(MagicMock(), MagicMock(), {"market": ToolNode([dummy_tool])},
                       ConditionalLogic()).setup_graph(["market"]).compile()
        fixed = {n for n in g.get_graph().nodes
                 if not n.startswith(("__", "Msg Clear", "tools_"))}
        assert fixed <= RESERVED_NODE_NAMES

    def test_equity_value_declares_quality_then_valuation(self):
        assert [a.key for a in EQUITY_VALUE.analysts] == ["quality", "valuation"]
        assert EQUITY_VALUE.analyst("valuation").label == "Valuation Analyst"
        assert EQUITY_VALUE.analyst("nope") is None

    def test_equity_momentum_declares_momentum_then_growth(self):
        assert [a.key for a in EQUITY_MOMENTUM.analysts] == ["momentum", "growth"]
        assert EQUITY_MOMENTUM.analyst("momentum").label == "Momentum Analyst"

    def test_the_two_mandates_share_no_analyst_keys(self):
        """Reports are keyed by analyst key in one state channel, so a shared key
        across mandates would be ambiguous the moment both could run."""
        assert not ({a.key for a in EQUITY_VALUE.analysts}
                    & {a.key for a in EQUITY_MOMENTUM.analysts})

    def test_each_momentum_analyst_sees_only_its_half_of_the_question(self):
        """Price and fundamental momentum disagreeing is itself the signal this
        mandate screens on; two analysts who can each see only one side cannot
        reconcile that disagreement away before the debate hears it."""
        tools = {a.key: {t.name for t in a.tools} for a in EQUITY_MOMENTUM.analysts}
        assert tools["momentum"] == {"get_relative_strength", "get_trend_structure"}
        assert tools["growth"] == {"get_growth_trajectory", "get_estimate_revisions"}
        assert not (tools["momentum"] & tools["growth"])


# --- the analyst node ------------------------------------------------------------


class _FakeLLM:
    """Captures the rendered prompt and returns scripted messages."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def bind_tools(self, tools):
        from langchain_core.runnables import RunnableLambda

        self.bound = [t.name for t in tools]
        return RunnableLambda(self._respond)

    def _respond(self, value):
        self.prompts.append(value)
        return self.responses.pop(0)


def _state(**extra):
    return {"messages": [("human", "KO")], "trade_date": "2026-09-17",
            "company_of_interest": "KO", "mandate": "equity_value",
            "mandate_context": render_mandate_context(EQUITY_VALUE), **extra}


class TestAnalystNode:

    def test_tool_round_returns_no_report(self):
        call = AIMessage(content="", tool_calls=[{"name": "dummy_tool", "args": {"ticker": "KO"}, "id": "1"}])
        node = create_mandate_analyst(analyst(), _FakeLLM(call))
        update = node(_state())
        assert "mandate_reports" not in update
        assert update["messages"] == [call]

    def test_final_round_reports_under_its_key(self):
        node = create_mandate_analyst(analyst(), _FakeLLM(AIMessage(content="The report.")))
        assert node(_state())["mandate_reports"] == {"alpha": "The report."}

    def test_content_blocks_are_flattened_to_text(self):
        blocks = [{"type": "text", "text": "Part one."}, {"type": "tool_use", "id": "x"},
                  {"type": "text", "text": "Part two."}]
        node = create_mandate_analyst(analyst(), _FakeLLM(AIMessage(content=blocks)))
        assert node(_state())["mandate_reports"]["alpha"] == "Part one.\nPart two."

    def test_prompt_carries_mandate_context_and_instructions(self):
        llm = _FakeLLM(AIMessage(content="ok"))
        create_mandate_analyst(analyst(msg="SPECIFIC INSTRUCTIONS"), llm)(_state())
        system = llm.prompts[0].to_messages()[0].content
        assert "INVESTMENT MANDATE: Long-Term Equity - Value / Quality" in system
        assert "SPECIFIC INSTRUCTIONS" in system
        assert "dummy_tool" in system and "2026-09-17" in system
        assert llm.bound == ["dummy_tool"]


# --- graph wiring ------------------------------------------------------------------


def _edges(mandate_analysts=(), selected=("market",)):
    tool_nodes = {k: ToolNode([dummy_tool]) for k in ANALYST_NODE_SPECS}
    g = GraphSetup(MagicMock(), MagicMock(), tool_nodes, ConditionalLogic()).setup_graph(
        list(selected), mandate_analysts=mandate_analysts).compile().get_graph()
    return {(e.source, e.target) for e in g.edges}


class TestWiring:

    def test_no_mandate_analysts_leaves_upstream_wiring_untouched(self):
        edges = _edges()
        assert ("Msg Clear Market", "Bull Researcher") in edges

    def test_mandate_analysts_run_after_upstream_and_before_the_debate(self):
        edges = _edges(EQUITY_VALUE.analysts, selected=("market", "fundamentals"))
        assert ("Msg Clear Fundamentals", "Quality Analyst") in edges
        assert ("Quality Analyst", "tools_quality") in edges
        assert ("tools_quality", "Quality Analyst") in edges
        assert ("Msg Clear Quality", "Valuation Analyst") in edges
        assert ("Msg Clear Valuation", "Bull Researcher") in edges
        assert ("Msg Clear Fundamentals", "Bull Researcher") not in edges

    def test_each_analyst_gets_only_its_own_tools(self):
        tool_nodes = {k: ToolNode([dummy_tool]) for k in ANALYST_NODE_SPECS}
        wf = GraphSetup(MagicMock(), MagicMock(), tool_nodes, ConditionalLogic()).setup_graph(
            ["market"], mandate_analysts=EQUITY_VALUE.analysts)
        quality_tools = set(wf.nodes["tools_quality"].runnable.tools_by_name)
        valuation_tools = set(wf.nodes["tools_valuation"].runnable.tools_by_name)
        assert quality_tools == {"get_quality_metrics", "get_capital_allocation"}
        assert valuation_tools == {"get_valuation_history", "get_reverse_dcf"}


# --- state and prompt reach ---------------------------------------------------------


class TestReach:

    def test_reducer_merges_reports_from_each_analyst(self):
        assert merge_mandate_reports({"quality": "Q"}, {"valuation": "V"}) == {"quality": "Q", "valuation": "V"}
        assert merge_mandate_reports(None, {"quality": "Q"}) == {"quality": "Q"}

    def test_initial_state_starts_with_no_mandate_reports(self):
        assert Propagator().create_initial_state("KO", "2026-09-17")["mandate_reports"] == {}

    def test_reports_render_in_the_mandates_order_with_labels(self):
        state = {"mandate": "equity_value", "mandate_reports": {"valuation": "V", "quality": "Q"}}
        assert [label for _, label, _ in iter_mandate_reports(state)] == ["Quality Analyst", "Valuation Analyst"]
        block = render_mandate_reports(state)
        assert block.index("Quality Analyst report:\nQ") < block.index("Valuation Analyst report:\nV")

    def test_unknown_mandate_still_renders_its_reports(self):
        state = {"mandate": "retired_style", "mandate_reports": {"thing": "T"}}
        assert list(iter_mandate_reports(state)) == [("thing", "thing", "T")]

    def test_empty_reports_render_nothing(self):
        assert render_mandate_reports({"mandate": "equity_value", "mandate_reports": {"quality": ""}}) == ""

    def test_downstream_agents_see_the_reports_through_mandate_section(self):
        state = _state(mandate_reports={"quality": "ROIC held above 13% for a decade."})
        section = mandate_section(state)
        assert section.startswith("INVESTMENT MANDATE")
        assert "Quality Analyst report:\nROIC held above 13% for a decade." in section

    def test_mandate_section_unchanged_before_any_report(self):
        state = _state()
        assert mandate_section(state) == f"{state['mandate_context']}\n\n"

    def test_unmandated_section_stays_empty(self):
        assert mandate_section({"mandate": "", "mandate_context": "", "mandate_reports": {}}) == ""

    def test_every_downstream_agent_reads_mandate_section(self):
        """The reports ride mandate_section; an agent that stopped reading it would go blind."""
        import inspect

        from tradingagents.agents.managers import portfolio_manager, research_manager
        from tradingagents.agents.researchers import bear_researcher, bull_researcher
        from tradingagents.agents.risk_mgmt import (
            aggressive_debator,
            conservative_debator,
            neutral_debator,
        )
        from tradingagents.agents.trader import trader

        for module in (bull_researcher, bear_researcher, research_manager, trader,
                       aggressive_debator, conservative_debator, neutral_debator, portfolio_manager):
            assert "mandate_section(" in inspect.getsource(module), module.__name__


# --- equity_value framing -------------------------------------------------------------


class TestValueFraming:

    def test_risk_is_framed_as_permanent_capital_loss(self):
        ctx = render_mandate_context(EQUITY_VALUE)
        assert "How to judge risk: risk is permanent loss of capital, not volatility" in ctx
        assert "not by price stops" in ctx

    def test_mandate_without_a_risk_frame_renders_no_risk_line(self):
        """Both shipped mandates now set one, so the absent path needs its own
        fixture rather than borrowing whichever mandate is least finished."""
        from tradingagents.mandates import Mandate

        bare = Mandate(name="bare", label="Bare", description="d")
        assert "How to judge risk" not in render_mandate_context(bare)

    def test_fundamentals_analyst_is_told_not_to_quote_ranges_from_memory(self):
        """The P1 KO run invented a 'normal 20-22x' P/E band; its computed range is 23-32x."""
        guidance = EQUITY_VALUE.guidance_for("fundamentals")
        assert "never quote a historical multiple or 'normal range' from memory" in guidance

    def test_market_analyst_leaves_valuation_to_the_valuation_analyst(self):
        assert "valuation multiples belong to the Valuation Analyst" in EQUITY_VALUE.guidance_for("market")


# --- saved report tree -----------------------------------------------------------------


def test_report_tree_writes_one_file_per_mandate_persona(tmp_path):
    from tradingagents.reporting import write_report_tree

    state = {"mandate": "equity_value", "market_report": "M",
             "mandate_reports": {"quality": "Q report", "valuation": "V report"}}
    complete = write_report_tree(state, "KO", tmp_path)
    assert (tmp_path / "1_analysts" / "quality.md").read_text() == "Q report"
    assert (tmp_path / "1_analysts" / "valuation.md").read_text() == "V report"
    text = complete.read_text()
    assert text.index("### Market Analyst") < text.index("### Quality Analyst") < text.index("### Valuation Analyst")


# --- CLI ---------------------------------------------------------------------------------


class TestCli:

    @pytest.fixture
    def buffer(self):
        from cli.main import MessageBuffer
        b = MessageBuffer()
        b.init_for_analysis(["market", "fundamentals"], mandate_analysts=EQUITY_VALUE.analysts)
        return b

    def test_personas_join_the_analyst_team(self, buffer):
        agents = list(buffer.agent_status)
        assert agents.index("Fundamentals Analyst") < agents.index("Quality Analyst") < agents.index("Valuation Analyst")
        assert agents.index("Valuation Analyst") < agents.index("Bull Researcher")

    def test_sections_sit_between_the_analysts_and_the_research_plan(self, buffer):
        sections = list(buffer.report_sections)
        assert sections.index("fundamentals_report") < sections.index("mandate:quality")
        assert sections.index("mandate:valuation") < sections.index("investment_plan")

    def test_class_mapping_is_not_mutated(self, buffer):
        from cli.main import MessageBuffer
        assert "mandate:quality" not in MessageBuffer.REPORT_SECTIONS

    def test_statuses_progress_through_the_mandate_analysts(self, buffer):
        from cli.main import update_analyst_statuses

        update_analyst_statuses(buffer, {"market_report": "M", "fundamentals_report": "F"})
        assert buffer.agent_status["Quality Analyst"] == "in_progress"
        assert buffer.agent_status["Bull Researcher"] == "pending"

        update_analyst_statuses(buffer, {"mandate_reports": {"quality": "Q"}})
        assert buffer.agent_status["Quality Analyst"] == "completed"
        assert buffer.agent_status["Valuation Analyst"] == "in_progress"
        assert buffer.agent_status["Bull Researcher"] == "pending"

        update_analyst_statuses(buffer, {"mandate_reports": {"quality": "Q", "valuation": "V"}})
        assert buffer.agent_status["Valuation Analyst"] == "completed"
        assert buffer.agent_status["Bull Researcher"] == "in_progress"

    def test_reports_render_and_count(self, buffer):
        from cli.main import update_analyst_statuses

        update_analyst_statuses(buffer, {"market_report": "M", "fundamentals_report": "F",
                                         "mandate_reports": {"quality": "Q"}})
        assert buffer.current_report.startswith("### Quality Analyst\nQ")
        assert "### Quality Analyst\nQ" in buffer.final_report
        assert buffer.get_completed_reports_count() == 3

    def test_no_mandate_keeps_the_upstream_layout(self):
        from cli.main import MessageBuffer
        b = MessageBuffer()
        b.init_for_analysis(["market"])
        assert b.mandate_analysts == []
        assert not any(s.startswith("mandate:") for s in b.report_sections)


# --- upstream preamble drift ----------------------------------------------


def _upstream_preamble(module) -> str:
    """The collaborative preamble as it stands in an upstream analyst module.

    Upstream inlines the text in each analyst rather than exporting a constant,
    so there is nothing to import. Adjacent string literals are folded by the
    parser, so the AST yields the assembled preamble as one constant.
    """
    import ast
    import inspect

    found = [
        node.value for node in ast.walk(ast.parse(inspect.getsource(module)))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "collaborating with other assistants" in node.value
    ]
    assert len(found) == 1, f"expected one preamble in {module.__name__}, got {len(found)}"
    return found[0]


class TestPreambleTracksUpstream:
    """Our copy of the preamble is the one thing in mandates/ that can silently
    drift from upstream, because it is a copy rather than an import.

    v0.5.0 replaced the old "FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**"
    instruction and our copy kept it, so all four mandate analysts went on being
    told to announce a trade call. The suite did not notice, exactly as it did
    not notice the two dropped hooks in the same merge.
    """

    def test_our_copy_is_identical_to_upstreams(self):
        import tradingagents.agents.analysts.market_analyst as market
        from tradingagents.mandates.analysts.base import _PREAMBLE

        assert _upstream_preamble(market) == _PREAMBLE, (
            "mandates/analysts/base.py::_PREAMBLE has drifted from the preamble in "
            "market_analyst.py. Copy upstream's text across and check whether the "
            "change also affects what the mandate analysts are told to produce."
        )

    def test_every_upstream_analyst_shares_that_preamble(self):
        """If upstream ever gives one analyst its own preamble, matching only
        the market analyst stops being a sufficient check."""
        import tradingagents.agents.analysts.fundamentals_analyst as fundamentals
        import tradingagents.agents.analysts.market_analyst as market
        import tradingagents.agents.analysts.news_analyst as news

        preambles = {_upstream_preamble(m) for m in (market, fundamentals, news)}
        assert len(preambles) == 1, "upstream analysts no longer share one preamble"

    def test_a_mandate_analyst_is_not_told_to_announce_a_trade(self):
        """The behavioural point, independent of the exact wording: these
        personas answer one part of the question and leave the call to the
        debate."""
        from tradingagents.mandates.analysts.base import _PREAMBLE

        assert "FINAL TRANSACTION PROPOSAL" not in _PREAMBLE
        assert "BUY/HOLD/SELL" not in _PREAMBLE
