"""Graph plumbing for mandate analysts.

Upstream builds its analyst chain from a fixed registry. These helpers splice a
mandate's analysts in after upstream's, before the research debate, and read
their reports back out -- so the upstream graph needs one hook to call
:func:`add_mandate_analysts`, and nothing else in it changes per mandate.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from .analysts.base import MandateAnalyst, create_mandate_analyst


def _router(spec: MandateAnalyst):
    """Tool loop until the analyst stops calling tools, then its message clear."""
    def route(state) -> str:
        last = state["messages"][-1]
        return spec.tool_node if getattr(last, "tool_calls", None) else spec.clear_node
    route.__name__ = f"should_continue_{spec.key}"
    return route


def add_mandate_analysts(workflow, analysts, llm, next_node: str) -> str:
    """Add each analyst's node, tool node and clear node, chained, ending at ``next_node``.

    Returns the node the preceding stage should hand to: the first mandate
    analyst, or ``next_node`` itself when the mandate adds none -- so an
    unmandated graph is wired exactly as upstream wires it.
    """
    from langgraph.prebuilt import ToolNode

    from tradingagents.agents.utils.agent_utils import create_msg_delete

    analysts = tuple(analysts)
    if not analysts:
        return next_node
    for spec in analysts:
        workflow.add_node(spec.label, create_mandate_analyst(spec, llm))
        workflow.add_node(spec.clear_node, create_msg_delete())
        workflow.add_node(spec.tool_node, ToolNode(list(spec.tools)))
        workflow.add_conditional_edges(spec.label, _router(spec), [spec.tool_node, spec.clear_node])
        workflow.add_edge(spec.tool_node, spec.label)
    for current, following in zip(analysts, analysts[1:], strict=False):
        workflow.add_edge(current.clear_node, following.label)
    workflow.add_edge(analysts[-1].clear_node, next_node)
    return analysts[0].label


def iter_mandate_reports(state: Mapping[str, Any]) -> Iterator[tuple[str, str, str]]:
    """``(key, label, report)`` for each finished mandate report, in the mandate's order.

    Falls back to the stored key as the label, and to insertion order, for a
    report from a mandate this build no longer knows -- a resumed checkpoint or
    an old state log must still render.
    """
    reports = state.get("mandate_reports") or {}
    if not reports:
        return
    from . import get_mandate

    try:
        mandate = get_mandate(state.get("mandate"))
    except ValueError:
        mandate = None
    ordered = [a.key for a in mandate.analysts] if mandate else []
    ordered += [k for k in reports if k not in ordered]
    for key in ordered:
        text = reports.get(key)
        if text:
            spec = mandate.analyst(key) if mandate else None
            yield key, spec.label if spec else key, text


def render_mandate_reports(state: Mapping[str, Any]) -> str:
    """The mandate analysts' reports as one prompt block, or ''."""
    parts = [f"{label} report:\n{text}" for _, label, text in iter_mandate_reports(state)]
    if not parts:
        return ""
    return "MANDATE ANALYST REPORTS\n\n" + "\n\n".join(parts)
