"""Apply a variant at call time, and capture what each agent was sent.

Installed only by an agent-lab run, in its own process; production runs never
import this. It wraps the chat model's ``invoke`` once, for every provider:

- the calling agent is the LangGraph node running the call;
- ``append`` / ``prepend`` edits go on that agent's system message (or its
  first message when it has none, as the researchers and risk analysts do);
- a ``replace`` edit rewrites an exact passage wherever it occurs in the
  messages; a passage that does not occur is counted as missed, never silently
  skipped, so a variant whose edit no longer matches the prompt shows it;
- the first prompt each agent receives for each case is written to
  ``captures.jsonl``, which is what the lab shows as "what the agent saw";
- tools named in ``extra_tools`` are bound to that analyst and executed by its
  tool node.
"""

from __future__ import annotations

import contextvars
import json
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .catalog import AGENTS
from .variants import Edit, Variant

CASE = contextvars.ContextVar("agentlab_case", default=("", ""))   # (ticker, date) being decided
NODE_OF_ANALYST = {a[3]: a[0] for a in AGENTS if a[3]}


def current_node() -> str | None:
    try:
        from langgraph.config import get_config

        return (get_config().get("metadata") or {}).get("langgraph_node")
    except Exception:  # outside a graph run
        return None


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


def _edit_content(content, fn):
    """Apply ``fn(str) -> str`` to a message's text, whatever shape it has."""
    if isinstance(content, str):
        return fn(content)
    if isinstance(content, list):
        out, done = [], False
        for b in content:
            if not done and isinstance(b, dict) and b.get("type") == "text":
                out.append({**b, "text": fn(b["text"])})
                done = True
            else:
                out.append(b)
        return out
    return content


def apply_edits(messages: list, node: str | None, edits: list[Edit]) -> tuple[list, list[int], list[int]]:
    """``(messages, applied edit indices, missed edit indices)`` for this node's call."""
    mine = [(i, e) for i, e in enumerate(edits) if node and (e.agent == "*" or e.agent == node)]
    if not mine or not messages:
        return messages, [], []
    msgs = list(messages)
    target = next((k for k, m in enumerate(msgs) if getattr(m, "type", "") == "system"), 0)
    applied, missed = [], []
    for i, e in mine:
        if e.kind in ("append", "prepend"):
            block = e.text.strip()
            fn = (lambda t, b=block: f"{t.rstrip()}\n\n{b}") if e.kind == "append" else (lambda t, b=block: f"{b}\n\n{t}")
            msgs[target] = msgs[target].model_copy(update={"content": _edit_content(msgs[target].content, fn)})
            applied.append(i)
            continue
        hit = False
        for k, m in enumerate(msgs):
            if e.find in _text_of(m.content):
                msgs[k] = m.model_copy(update={"content": _edit_content(m.content, lambda t, f=e.find, r=e.text: t.replace(f, r))})
                hit = True
        (applied if hit else missed).append(i)
    return msgs, applied, missed


def _subclasses(cls) -> set:
    out = set()
    for sub in cls.__subclasses__():
        out.add(sub)
        out |= _subclasses(sub)
    return out


class _State:
    def __init__(self, variant: Variant, capture: Path | None):
        self.variant = variant
        self.capture = capture
        self.captured: set[tuple] = set()
        self.applied: Counter = Counter()
        self.missed: Counter = Counter()
        self.lock = threading.Lock()


_STATE: _State | None = None


def _record(node: str, model: str, messages: list, applied: list[int], missed: list[int]) -> None:
    s = _STATE
    if s is None:
        return
    ticker, day = CASE.get()
    with s.lock:
        for i in applied:
            s.applied[i] += 1
        for i in missed:
            s.missed[i] += 1
        key = (node, ticker, day)
        if s.capture is None or key in s.captured:
            return
        s.captured.add(key)
        row = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "node": node, "ticker": ticker,
               "date": day, "model": model, "applied": applied, "missed": missed,
               "messages": [{"role": getattr(m, "type", "?"), "text": _text_of(m.content)} for m in messages]}
        with s.capture.open("a") as fh:
            fh.write(json.dumps(row) + "\n")


@contextmanager
def installed(variant: Variant, capture: Path | None = None):
    """Apply ``variant`` to every model call made inside this block."""
    global _STATE
    from langchain_core.language_models.chat_models import BaseChatModel

    from tradingagents.graph.trading_graph import TradingAgentsGraph

    tools = {}
    if variant.extra_tools:
        from .catalog import all_tools

        defined = all_tools()
        tools = {key: [defined[t] for t in names] for key, names in variant.extra_tools.items()}

    orig_invoke, orig_ainvoke = BaseChatModel.invoke, BaseChatModel.ainvoke
    # Providers override bind_tools (ChatAnthropic does), so each class that
    # defines its own is wrapped, not just the base.
    binders = [c for c in _subclasses(BaseChatModel) | {BaseChatModel} if "bind_tools" in c.__dict__]
    orig_binds = {c: c.__dict__["bind_tools"] for c in binders}
    orig_nodes, orig_propagate = TradingAgentsGraph._create_tool_nodes, TradingAgentsGraph.propagate

    def _prepare(self, input):
        node = current_node()
        msgs = self._convert_input(input).to_messages()
        msgs, applied, missed = apply_edits(msgs, node, variant.edits)
        if node:
            _record(node, getattr(self, "model", "") or getattr(self, "model_name", ""), msgs, applied, missed)
        return msgs

    def invoke(self, input, config=None, **kw):
        return orig_invoke(self, _prepare(self, input), config, **kw)

    async def ainvoke(self, input, config=None, **kw):
        return await orig_ainvoke(self, _prepare(self, input), config, **kw)

    def binder(orig):
        def bind_tools(self, tool_list, *a, **kw):
            node = current_node()
            extra = next((ts for key, ts in tools.items() if NODE_OF_ANALYST.get(key) == node), [])
            names = {getattr(t, "name", None) for t in tool_list}
            return orig(self, list(tool_list) + [t for t in extra if t.name not in names], *a, **kw)
        return bind_tools

    def create_tool_nodes(self):
        from langgraph.prebuilt import ToolNode

        nodes = orig_nodes(self)
        for key, ts in tools.items():
            if key in nodes:
                have = nodes[key].tools_by_name
                nodes[key] = ToolNode(list(have.values()) + [t for t in ts if t.name not in have])
        return nodes

    def propagate(self, company_name, trade_date, *a, **kw):
        token = CASE.set((str(company_name), str(trade_date)))
        try:
            return orig_propagate(self, company_name, trade_date, *a, **kw)
        finally:
            CASE.reset(token)

    _STATE = _State(variant, capture)
    BaseChatModel.invoke, BaseChatModel.ainvoke = invoke, ainvoke
    if tools:
        for c, orig in orig_binds.items():
            c.bind_tools = binder(orig)
    TradingAgentsGraph._create_tool_nodes, TradingAgentsGraph.propagate = create_tool_nodes, propagate
    try:
        yield _STATE
    finally:
        BaseChatModel.invoke, BaseChatModel.ainvoke = orig_invoke, orig_ainvoke
        for c, orig in orig_binds.items():
            c.bind_tools = orig
        TradingAgentsGraph._create_tool_nodes, TradingAgentsGraph.propagate = orig_nodes, orig_propagate
        _STATE = None
