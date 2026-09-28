"""Can the models be called? One tiny request, before a night spends anything.

On 2026-09-28 the Anthropic credit balance ran out at 02:45: every later call
was refused, each name failed on its own, and nothing said why until someone
read the log. ``check`` makes one request of a single output token (a fraction
of a cent) and says which of four things is true: the API answers, the credit
balance is empty, the key is refused, or the API can't be reached.

Used by ``tradingagents llm-check`` (the nightly script runs it before any
model step and skips them when credits or the key are the problem), by the
evening watcher (``tradingagents watch credits``, so there is time to top up
before 02:00), and by the sweep loop, which stops at the first out-of-credits
failure instead of failing every remaining name.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

OUT_OF_CREDITS = re.compile(r"credit balance is too low", re.I)
TOP_UP = "top up at console.anthropic.com -> Plans & Billing (auto-reload stops it happening mid-run)"

# Exit codes for `tradingagents llm-check`.
EXIT = {"ok": 0, "skipped": 0, "no_credits": 3, "auth": 4, "unreachable": 5, "error": 6}


def is_out_of_credits(error: object) -> bool:
    return bool(OUT_OF_CREDITS.search(str(error)))


@dataclass(frozen=True)
class Check:
    status: str          # ok | skipped | no_credits | auth | unreachable | error
    model: str
    detail: str = ""

    @property
    def blocks_models(self) -> bool:
        """Whether model steps should be skipped: they would all fail the same way."""
        return self.status in ("no_credits", "auth")

    def line(self) -> str:
        if self.status == "ok":
            return f"LLM check: ok ({self.model} answered)"
        if self.status == "skipped":
            return f"LLM check: skipped ({self.detail})"
        what = {"no_credits": f"the Anthropic credit balance is too low; {TOP_UP}",
                "auth": "the Anthropic API key was refused; check ANTHROPIC_API_KEY in .env",
                "unreachable": "the Anthropic API could not be reached",
                "error": "the test call failed"}[self.status]
        return f"LLM check: FAILED -- {what}" + (f" ({self.detail})" if self.detail else "")


def _call_anthropic(model: str) -> None:
    import anthropic

    anthropic.Anthropic(max_retries=0, timeout=30).messages.create(
        model=model, max_tokens=1, messages=[{"role": "user", "content": "ok"}])


def check(config: dict | None = None, call: Callable[[str], None] | None = None,
          attempts: int = 3, wait: float = 20.0) -> Check:
    """One single-token request to the quick model; unreachable is retried (a Mac just woken)."""
    if config is None:
        from tradingagents.default_config import DEFAULT_CONFIG as config
    provider = str(config.get("llm_provider", "")).lower()
    model = str(config.get("quick_think_llm", ""))
    if provider != "anthropic":
        return Check("skipped", model, f"provider {provider or 'unset'} is not checked")
    call = call or _call_anthropic
    last = ""
    for n in range(attempts):
        try:
            call(model)
            return Check("ok", model)
        except Exception as exc:  # classified below; nothing here should crash the night
            name, text = type(exc).__name__, str(exc)
            if is_out_of_credits(text):
                return Check("no_credits", model)
            if name in ("AuthenticationError", "PermissionDeniedError"):
                return Check("auth", model, name)
            if name in ("APIConnectionError", "APITimeoutError", "InternalServerError", "OverloadedError",
                        "RateLimitError", "ServiceUnavailableError"):
                last = name
                if n + 1 < attempts:
                    time.sleep(wait)
                continue
            return Check("error", model, f"{name}: {text[:200]}")
    return Check("unreachable", model, last)
