"""Prompt caching for the analysts' tool loops (Anthropic).

A decision is about twenty model calls. The four analysts call tools in a
loop, and every turn re-sends the whole conversation so far plus the newest
tool result, so each turn's prompt starts with the previous turn's. Those
loops are where caching pays: a cache write costs 1.25x the input price and a
read 0.1x, and each prefix is written once and read on every later turn.

Every other call (the debate, the trader, the managers, structured output) is
a single prompt that opens with its own role text: nothing would read its
cache, so caching it would only add the 25% write premium. So the breakpoint
goes only on calls made with tools bound and no forced ``tool_choice``,
which is what structured output uses.

The request carries the API's top-level ``cache_control`` (automatic
caching: the breakpoint lands on the last cacheable block). Prompts under the
model's minimum cacheable length are simply not cached, and not charged extra.
"""

from __future__ import annotations

from typing import Any

from .anthropic_client import NormalizedChatAnthropic

EPHEMERAL = {"type": "ephemeral"}


def caches(tool_choice: Any, kwargs: dict) -> bool:
    """Whether a ``bind_tools`` call is a tool loop (cache) or structured output (don't)."""
    return tool_choice is None and "ls_structured_output_format" not in kwargs


class CachingChatAnthropic(NormalizedChatAnthropic):
    """NormalizedChatAnthropic that caches the prefix of tool-loop calls."""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        if caches(tool_choice, kwargs):
            kwargs.setdefault("cache_control", EPHEMERAL)
        return super().bind_tools(tools, tool_choice=tool_choice, **kwargs)
