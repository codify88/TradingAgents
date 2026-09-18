# TradingAgents/graph/reflection.py

from typing import Any


class Reflector:
    """Handles reflection on trading decisions."""

    def __init__(self, quick_thinking_llm: Any):
        """Initialize the reflector with an LLM."""
        self.quick_thinking_llm = quick_thinking_llm
        self.log_reflection_prompt = self._get_log_reflection_prompt()
        self.interim_review_prompt = self._get_interim_review_prompt()

    def _get_log_reflection_prompt(self) -> str:
        """Concise prompt for reflect_on_final_decision (Phase B log entries).

        Produces 2-4 sentences of plain prose — compact enough to be re-injected
        into future agent prompts without bloating the context window.
        """
        return (
            "You are a trading analyst reviewing your own past decision now that the outcome is known.\n"
            "Write exactly 2-4 sentences of plain prose (no bullets, no headers, no markdown).\n\n"
            "Cover in order:\n"
            "1. Was the directional call correct? (cite the alpha figure)\n"
            "2. Which part of the investment thesis held or failed?\n"
            "3. One concrete lesson to apply to the next similar analysis.\n\n"
            "Be specific and terse. Your output will be stored verbatim in a decision log "
            "and re-read by future analysts, so every word must earn its place."
        )

    def _get_interim_review_prompt(self) -> str:
        """Prompt for an interim checkpoint on a thesis that has not settled.

        Deliberately different from the final reflection: at 63 days of a
        504-day thesis the return is not a verdict, and a prompt that asks
        "were you right?" would manufacture one. Asking what is *tracking*
        keeps the lesson honest and keeps the log from teaching impatience.
        """
        return (
            "You are reviewing an open investment thesis at a scheduled interim checkpoint. "
            "The position has NOT reached its evaluation horizon, so the outcome is not yet "
            "known and you must not declare the call right or wrong.\n"
            "Write exactly 2-4 sentences of plain prose (no bullets, no headers, no markdown).\n\n"
            "Cover in order:\n"
            "1. Is the thesis tracking, drifting, or breaking so far? (cite the alpha figure, "
            "and weigh it against how much of the horizon has actually elapsed)\n"
            "2. Which specific claim in the thesis this checkpoint supports or undercuts.\n"
            "3. What would have to happen by the next checkpoint to confirm or kill it.\n\n"
            "A small move over a short slice of a long horizon is usually noise -- say so "
            "plainly when it is, rather than inventing a signal. Your output is stored in a "
            "decision log and re-read by future analysts, so every word must earn its place."
        )

    def reflect_on_interim_outcome(
        self,
        final_decision: str,
        raw_return: float,
        alpha_return: float,
        elapsed_days: int,
        horizon_days: int,
        benchmark_name: str = "SPY",
    ) -> str:
        """Interim checkpoint note for a decision that is still open.

        ``elapsed_days`` of ``horizon_days`` have passed. Both are stated in the
        prompt so the model can scale its confidence to the fraction of the
        horizon actually elapsed.
        """
        elapsed_pct = elapsed_days / horizon_days if horizon_days else 0.0
        messages = [
            ("system", self.interim_review_prompt),
            (
                "human",
                (
                    f"Checkpoint: {elapsed_days} of {horizon_days} trading days elapsed "
                    f"({elapsed_pct:.0%} of the horizon).\n"
                    f"Raw return so far: {raw_return:+.1%}\n"
                    f"Alpha vs {benchmark_name} so far: {alpha_return:+.1%}\n\n"
                    f"Original Decision:\n{final_decision}"
                ),
            ),
        ]
        return self.quick_thinking_llm.invoke(messages).content

    def reflect_on_final_decision(
        self,
        final_decision: str,
        raw_return: float,
        alpha_return: float,
        benchmark_name: str = "SPY",
    ) -> str:
        """Single reflection call on the final trade decision with outcome context.

        Used by Phase B deferred reflection. The final_trade_decision already
        synthesises all analyst insights, so no separate market context is needed.
        ``benchmark_name`` is the label used for the alpha line (e.g. ``"SPY"``
        for US tickers, ``"^N225"`` for ``.T`` listings); defaults to SPY for
        callers that haven't been updated to thread the benchmark through.
        """
        messages = [
            ("system", self.log_reflection_prompt),
            (
                "human",
                (
                    f"Raw return: {raw_return:+.1%}\n"
                    f"Alpha vs {benchmark_name}: {alpha_return:+.1%}\n\n"
                    f"Final Decision:\n{final_decision}"
                ),
            ),
        ]
        return self.quick_thinking_llm.invoke(messages).content
