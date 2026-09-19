"""The shape of a mandate-supplied analyst, and the node that runs one.

A mandate analyst runs exactly like an upstream analyst -- system prompt, bound
tools, a tool loop, a message clear -- but it is *declared* by a mandate rather
than hard-wired into the graph. Its report lands in ``state["mandate_reports"]``
under its key, and reaches every downstream agent through ``mandate_section``,
which each of them already interpolates. Adding the next mandate's analysts
(P3's Momentum and Growth) therefore touches nothing upstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Verbatim copy of the preamble every upstream analyst uses, so a mandate
# analyst behaves identically in the tool loop. It has to be a copy -- upstream
# inlines the text in each analyst module rather than exporting a constant --
# which makes it a drift risk on every merge: v0.5.0 replaced the old
# "FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**" instruction with the line
# below, and this copy silently kept telling four mandate analysts to announce
# a trade call. A Quality Analyst emitting BUY/HOLD/SELL is worse here than it
# was upstream: these personas exist precisely to answer one part of the
# question and leave the call to the debate.
#
# tests/test_mandate_analysts.py asserts this matches the text in
# market_analyst.py, so the next upstream edit fails loudly instead.
_PREAMBLE = (
    "You are a helpful AI assistant, collaborating with other assistants."
    " Use the provided tools to progress towards answering the question."
    " If you are unable to fully answer, that's OK; another assistant with different tools"
    " will help where you left off. Execute what you can to make progress."
    " Report what your tools support; another agent decides the trade."
    " You have access to the following tools: {tool_names}."
    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
    "{system_message}"
)


@dataclass(frozen=True)
class MandateAnalyst:
    """A persona a mandate adds to the analyst team."""

    key: str            # wire key: mandate_reports key and report filename
    label: str          # graph node name and persona title, e.g. "Quality Analyst"
    tools: tuple        # LangChain tools bound to this analyst
    system_message: str

    def __post_init__(self):
        if not self.key.isidentifier():
            raise ValueError(f"mandate analyst key {self.key!r} must be an identifier")
        if not self.label.endswith(" Analyst"):
            raise ValueError(f"mandate analyst label {self.label!r} must end in ' Analyst'")
        if not self.tools:
            raise ValueError(f"mandate analyst {self.key!r} needs at least one tool")

    @property
    def clear_node(self) -> str:
        return f"Msg Clear {self.label.removesuffix(' Analyst')}"

    @property
    def tool_node(self) -> str:
        return f"tools_{self.key}"


def _text(content: Any) -> str:
    """Report text from a message's content, whether a string or content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
            if not isinstance(block, dict) or block.get("type") == "text"
        ).strip()
    return str(content or "")


def create_mandate_analyst(spec: MandateAnalyst, llm):
    """Build the graph node for one mandate analyst."""
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    from tradingagents.agents.utils.agent_utils import (
        apply_mandate_to_system_message,
        get_instrument_context_from_state,
        get_language_instruction,
    )

    tools = list(spec.tools)

    def mandate_analyst_node(state):
        prompt = ChatPromptTemplate.from_messages(
            [("system", _PREAMBLE), MessagesPlaceholder(variable_name="messages")]
        ).partial(
            system_message=apply_mandate_to_system_message(
                state, spec.key, spec.system_message + get_language_instruction()
            ),
            tool_names=", ".join(t.name for t in tools),
            current_date=state["trade_date"],
            instrument_context=get_instrument_context_from_state(state),
        )
        result = (prompt | llm.bind_tools(tools)).invoke(state["messages"])

        update = {"messages": [result]}
        if not result.tool_calls:
            update["mandate_reports"] = {spec.key: _text(result.content)}
        return update

    return mandate_analyst_node
