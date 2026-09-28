"""Prompt caching goes on the analysts' tool loops and nowhere else."""

from __future__ import annotations

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from tradingagents.agentlab.runs import cost_range
from tradingagents.llm_clients.anthropic_client import AnthropicClient, NormalizedChatAnthropic
from tradingagents.llm_clients.prompt_cache import CachingChatAnthropic


@tool
def get_prices(symbol: str) -> str:
    """Daily prices."""
    return symbol


class Rating(BaseModel):
    rating: str


def _payload(llm, runnable):
    return llm._get_request_payload([HumanMessage("hi")], **runnable.kwargs)


def test_a_tool_loop_carries_the_breakpoint():
    llm = CachingChatAnthropic(model="claude-haiku-4-5", api_key="x")
    assert _payload(llm, llm.bind_tools([get_prices]))["cache_control"] == {"type": "ephemeral"}


def test_structured_output_and_forced_tools_do_not():
    llm = CachingChatAnthropic(model="claude-haiku-4-5", api_key="x")
    assert "cache_control" not in _payload(llm, llm.bind_tools([get_prices], tool_choice="get_prices"))
    first = llm.with_structured_output(Rating).first
    assert "cache_control" not in first.kwargs


def test_the_client_caches_only_when_asked():
    assert type(AnthropicClient("claude-haiku-4-5", api_key="x", prompt_cache=True).get_llm()) is CachingChatAnthropic
    assert type(AnthropicClient("claude-haiku-4-5", api_key="x").get_llm()) is NormalizedChatAnthropic


def test_the_graph_passes_the_setting():
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    g = TradingAgentsGraph.__new__(TradingAgentsGraph)
    g.config = {"llm_provider": "anthropic", "anthropic_prompt_cache": True}
    assert g._get_provider_kwargs()["prompt_cache"] is True
    g.config = {"llm_provider": "anthropic", "anthropic_prompt_cache": False}
    assert "prompt_cache" not in g._get_provider_kwargs()


def test_cached_tokens_are_priced_at_their_rates():
    # Haiku: $1 in, $5 out. 1M in of which 600k read and 100k written; 0 out.
    low, high = cost_range(1_000_000, 0, ["claude-haiku-4-5"], cache_read=600_000, cache_write=100_000)
    assert low == high == (300_000 + 60_000 + 125_000) / 1e6
    assert cost_range(1_000_000, 0, ["claude-haiku-4-5"]) == (1.0, 1.0)


def test_cache_writes_are_counted_when_reported_per_lifetime():
    from tradingagents.usage import cache_written

    assert cache_written({"cache_creation": 0, "ephemeral_5m_input_tokens": 19553, "ephemeral_1h_input_tokens": 0}) == 19553
    assert cache_written({"cache_creation": 120}) == 120
    assert cache_written({}) == 0
