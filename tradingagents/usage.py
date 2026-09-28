"""Model usage per backtest cell, so a run's cost is measured rather than guessed.

The interactive CLI counts tokens for its live view (``cli/stats_handler.py``);
sweeps and the nightly job had nothing, so "about 600k tokens a name" was an
estimate. This tracker is attached to a sweep's model clients and read around
each cell, and ``run_backtest`` writes one row per cell to ``usage.jsonl``.

Cache reads and writes are reported alongside the input total (langchain's
``input_tokens`` already includes them): with prompt caching they are priced
differently, and a change that moves tokens between them changes the bill
without changing the total.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult


@dataclass(frozen=True)
class Usage:
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def __sub__(self, other: Usage) -> Usage:
        return Usage(*(a - b for a, b in zip(self.astuple(), other.astuple(), strict=True)))

    def __add__(self, other: Usage) -> Usage:
        return Usage(*(a + b for a, b in zip(self.astuple(), other.astuple(), strict=True)))

    def astuple(self) -> tuple[int, ...]:
        return (self.llm_calls, self.tokens_in, self.tokens_out, self.cache_read, self.cache_write)

    def asdict(self) -> dict[str, int]:
        return asdict(self)


def cache_written(details: dict) -> int:
    """Cache-write tokens. langchain-anthropic reports them per lifetime
    (``ephemeral_5m_input_tokens`` / ``ephemeral_1h_input_tokens``) and then
    zeroes the generic ``cache_creation``, so read the lifetimes first."""
    by_ttl = sum(int(details.get(k) or 0) for k in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"))
    return by_ttl or int(details.get("cache_creation") or 0)


class UsageTracker(BaseCallbackHandler):
    """Cumulative usage across every model call made through the clients it is bound to."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._total = Usage()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        found = Usage()
        for generations in response.generations or []:
            for generation in generations:
                meta = getattr(getattr(generation, "message", None), "usage_metadata", None)
                if not meta:
                    continue
                details = meta.get("input_token_details") or {}
                found = found + Usage(
                    llm_calls=0,
                    tokens_in=int(meta.get("input_tokens") or 0),
                    tokens_out=int(meta.get("output_tokens") or 0),
                    cache_read=int(details.get("cache_read") or 0),
                    cache_write=cache_written(details),
                )
        with self._lock:
            self._total = self._total + found + Usage(llm_calls=1)

    def snapshot(self) -> Usage:
        with self._lock:
            return self._total


def render_usage(rows: list[dict]) -> str:
    """One line for a sweep's summary: totals and the mean per cell that ran."""
    cells = [r for r in rows if r.get("kind") == "cell"]
    if not cells:
        return ""
    total = Usage()
    for r in cells:
        total = total + Usage(r["llm_calls"], r["tokens_in"], r["tokens_out"],
                              r["cache_read"], r["cache_write"])
    n = len(cells)
    return (
        f"Model usage: {n} cell(s), {total.tokens_in:,} tokens in "
        f"(of which {total.cache_read:,} cache read, {total.cache_write:,} cache write), "
        f"{total.tokens_out:,} out, {total.llm_calls:,} calls; "
        f"per cell {total.tokens_in // n:,} in / {total.tokens_out // n:,} out."
    )
