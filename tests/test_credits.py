"""The credit check: say why the models can't be called, before a night spends anything."""

from __future__ import annotations

import pytest

from tradingagents.ops import credits

CFG = {"llm_provider": "anthropic", "quick_think_llm": "claude-sonnet-5"}
BROKE = ("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': "
         "'Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing'}}")


def raiser(name: str, text: str = "x"):
    cls = type(name, (Exception,), {})

    def call(model):
        raise cls(text)
    return call


@pytest.mark.parametrize("call,status,blocks", [
    (lambda m: None, "ok", False),
    (raiser("BadRequestError", BROKE), "no_credits", True),
    (raiser("AuthenticationError"), "auth", True),
    (raiser("APIConnectionError"), "unreachable", False),
    (raiser("ValueError", "odd"), "error", False),
])
def test_each_outcome(call, status, blocks):
    c = credits.check(CFG, call=call, wait=0)
    assert c.status == status and c.blocks_models is blocks
    assert c.line().startswith("LLM check: ok" if status == "ok" else "LLM check: FAILED")


def test_unreachable_is_retried_then_recovers():
    tries = []

    def call(model):
        tries.append(model)
        if len(tries) < 3:
            raise type("APIConnectionError", (Exception,), {})("down")
    assert credits.check(CFG, call=call, wait=0).status == "ok" and len(tries) == 3


def test_other_providers_are_not_checked():
    assert credits.check({"llm_provider": "openai", "quick_think_llm": "x"}, call=raiser("X")).status == "skipped"


def test_the_out_of_credits_line_says_how_to_fix_it():
    assert "console.anthropic.com" in credits.check(CFG, call=raiser("BadRequestError", BROKE)).line()


def test_a_sweep_stops_at_the_first_out_of_credits_failure(tmp_path, monkeypatch):
    import tradingagents.backtest as bt

    calls, settled = [], []

    class Graph:
        mandate_name = ""

        def __init__(self, *a, **kw):
            self.memory_log = type("L", (), {"load_entries": lambda self: []})()

        def propagate(self, ticker, date, *a, **kw):
            calls.append(ticker)
            raise RuntimeError(BROKE)

        def settle_pending(self, ticker):
            settled.append(ticker)

    monkeypatch.setattr(bt, "TradingAgentsGraph", Graph)
    r = bt.run_backtest(["AAA", "BBB", "CCC"], ["2026-09-28"], config={"results_dir": str(tmp_path)})
    assert calls == ["AAA"] and len(r.failures) == 1 and settled == []


def test_the_nightly_log_names_the_problem(tmp_path):
    from tradingagents.ops.nightly_log import parse

    log = tmp_path / "20260929_020002.log"
    log.write_text("=== nightly run 20260929_020002 ===\n"
                   "LLM check: FAILED -- the Anthropic credit balance is too low; top up at console.anthropic.com\n"
                   "=== finished 02:40:00, screen-run exit=3 ===\n")
    assert parse(log).problems()[0].startswith("the Anthropic credit balance is too low")
    log2 = tmp_path / "20260928_020002.log"
    log2.write_text("failed: MU 2026-09-28: Error code: 400 - Your credit balance is too low to access -- run again to retry\n")
    assert "ran out during the run" in parse(log2).problems()[0]


def test_the_watcher_is_silent_when_fine(monkeypatch):
    from tradingagents.ops import watch

    monkeypatch.setattr(credits, "check", lambda *a, **k: credits.Check("ok", "m"))
    assert watch.credits() == ""
    monkeypatch.setattr(credits, "check", lambda *a, **k: credits.Check("no_credits", "m"))
    assert "can't call the models" in watch.credits()
