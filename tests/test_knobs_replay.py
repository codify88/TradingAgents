"""Knobs and replays: the tools for the agents' rating habit."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import pytest

from tradingagents.agentlab import knobs, replay, variants as va

ROOT = Path(__file__).resolve().parents[1] / "tradingagents"


@pytest.fixture
def config(tmp_path):
    return {"data_cache_dir": str(tmp_path / "cache"), "results_dir": str(tmp_path / "results"),
            "deep_think_llm": "claude-opus-4-8", "quick_think_llm": "claude-sonnet-5", "llm_provider": "anthropic",
            "max_risk_discuss_rounds": 1, "max_debate_rounds": 1}


def _source(rel: str) -> str:
    text = (ROOT / rel).read_text()
    return re.sub(r'"\s*\n\s*"', "", text)      # join implicitly concatenated string literals


@pytest.mark.parametrize("passage,source", [
    (knobs.PM_SCALE, "agents/managers/portfolio_manager.py"),
    (knobs.RM_SCALE, "agents/managers/research_manager.py"),
    (knobs.PORTFOLIO_UNKNOWN, "agents/utils/agent_utils.py"),
    (knobs.CONSERVATIVE_OPENING, "agents/risk_mgmt/conservative_debator.py"),
])
def test_every_replaced_passage_is_still_in_its_prompt(passage, source):
    """A knob whose passage drifted would silently do nothing (the hook counts it as missed)."""
    assert passage in _source(source)


def test_production_options_change_nothing():
    v = knobs.compose("prod", {})
    assert v.edits == [] and v.settings == {} and v.knobs == {}
    for k in knobs.KNOBS:
        assert not k.options[0].edits and not k.options[0].settings


def test_compose_collects_edits_settings_and_choices():
    v = knobs.compose("entry-flat", {"scale": "entry", "book": "flat", "risk_rounds": "2"},
                      models={"deep": "claude-haiku-4-5", "quick": ""})
    assert {(e.agent, e.kind) for e in v.edits} == {("Portfolio Manager", "replace"), ("Research Manager", "replace"),
                                                    ("*", "replace")}
    assert v.settings == {"max_risk_discuss_rounds": 2} and v.models == {"deep": "claude-haiku-4-5"}
    assert v.knobs == {"scale": "entry", "book": "flat", "risk_rounds": "2"} and "Entry decision" in v.description
    assert va.validate(v) == []
    with pytest.raises(ValueError):
        knobs.compose("x", {"scale": "nope"})
    with pytest.raises(ValueError):
        knobs.compose("x", {"volume": "11"})


def test_the_hook_applies_a_knob_to_the_real_prompt_text():
    from langchain_core.messages import HumanMessage

    from tradingagents.agentlab.hook import apply_edits

    v = knobs.compose("e", {"scale": "entry", "book": "flat"})
    msg = HumanMessage(f"{knobs.PORTFOLIO_UNKNOWN}\n\n---\n\n{knobs.PM_SCALE}\n\nRest.")
    out, applied, missed = apply_edits([msg], "Portfolio Manager", v.edits)
    assert "new capital today" in out[0].content and "cash to deploy" in out[0].content
    assert applied and not missed


# --- replay -----------------------------------------------------------------------


def _saved_case(config, run="scr_x", ticker="AAA", day="2025-09-05", rating="Hold", mandate="equity_value"):
    base = Path(config["results_dir"]) / "backtest" / run
    logs = base / ticker / "TradingAgentsStrategy_logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / f"full_states_log_{day}.json").write_text(json.dumps({
        "company_of_interest": ticker, "trade_date": day, "market_report": "M", "sentiment_report": "S",
        "news_report": "N", "fundamentals_report": "F", "mandate_reports": {"quality": "Q"},
        "investment_debate_state": {"bull_history": "B", "bear_history": "b", "history": "Bb", "current_response": "",
                                    "judge_decision": "RM"},
        "trader_investment_decision": "TRADE", "investment_plan": "PLAN",
        "risk_debate_state": {"aggressive_history": "A", "conservative_history": "C", "neutral_history": "Nn",
                              "history": "ACN", "judge_decision": "PM"},
        "final_trade_decision": f"**Rating**: {rating}"}))
    tag = f" | mandate:{mandate}" if mandate else ""
    with (base / "trading_memory.md").open("a") as fh:
        fh.write(f"[{day} | {ticker} | {rating} | +1.0% | +0.5% | 5d | resolved:2025-09-12{tag}]\n\nDECISION:\n"
                 f"**Rating**: {rating}\n\n<!-- ENTRY_END -->\n\n")


def test_cases_come_from_saved_states_with_their_mandate_and_rating(config):
    _saved_case(config, ticker="AAA", rating="Hold")
    _saved_case(config, ticker="BBB", rating="Underweight", mandate="")
    cs = replay.all_cases(config)
    assert [(c.ticker, c.mandate, c.rating) for c in cs] == [("AAA", "equity_value", "Hold"), ("BBB", "", "Underweight")]
    assert replay.case_sets(config)["by_mandate"] == {"equity_value": {"Hold": 1}, "none": {"Underweight": 1}}
    assert [c.ticker for c in replay.choose(config, "none", 10)] == ["BBB"]
    assert replay.choose(config, "any", 2, seed=1) == replay.choose(config, "any", 2, seed=1)


def test_state_for_each_stage_carries_only_what_came_before(config):
    _saved_case(config)
    c = replay.all_cases(config)[0]
    saved = json.loads(Path(c.path).read_text())
    pm = replay._state(saved, "pm", c, "CTX", 1)
    assert pm["risk_debate_state"]["history"] == "ACN" and pm["risk_debate_state"]["count"] == 3
    assert pm["trader_investment_plan"] == "TRADE" and pm["mandate_context"] == "CTX" and pm["mandate"] == "equity_value"
    risk = replay._state(saved, "risk", c, "CTX", 1)
    assert risk["risk_debate_state"]["history"] == "" and risk["trader_investment_plan"] == "TRADE"
    research = replay._state(saved, "research", c, "CTX", 1)
    assert research["investment_plan"] == "" and research["investment_debate_state"]["history"] == ""
    assert research["mandate_reports"] == {"quality": "Q"} and research["market_report"] == "M"


def test_outcomes_are_open_to_open_alpha_after_the_decision(config):
    from types import SimpleNamespace

    days = pd.bdate_range("2025-09-01", periods=12)
    opens = pd.DataFrame({"AAA": [100.0] * 5 + [110.0] * 7, "SPY": [100.0] * 5 + [101.0] * 7}, index=days)
    panel = SimpleNamespace(open=opens)
    c = replay.Case("r/AAA/2025-09-04", "r", "AAA", "2025-09-04", "", "Hold", "")
    # Decided on the 4th: in at the 5th's open (100), out 5 sessions later (110); SPY 100 -> 101.
    assert replay.outcomes(config, [c], 5, panel=panel)["r/AAA/2025-09-04"] == pytest.approx(0.10 - 0.01)
    late = replay.Case("r/AAA/2025-09-15", "r", "AAA", "2025-09-15", "", "Hold", "")
    assert replay.outcomes(config, [late], 5, panel=panel) == {}


def test_a_replay_runs_the_tail_under_the_variant_and_scores_it(config, monkeypatch):
    """The real tail graph and the real hook, with a fake model: the knob's text reaches the PM."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from tradingagents.agentlab.hook import current_node

    _saved_case(config, ticker="AAA", rating="Hold")
    _saved_case(config, ticker="BBB", rating="Hold")
    seen = []

    class Model(GenericFakeChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kw):
            seen.append((current_node(), messages[-1].content))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kw)

        def with_structured_output(self, *a, **k):
            raise NotImplementedError   # the PM falls back to free text

    def factory(cfg, mandate, tracker):
        from types import SimpleNamespace

        llm = Model(messages=iter([AIMessage("**Rating**: Overweight\n\nProbability of beating the benchmark: 61%.")] * 10))
        return SimpleNamespace(deep_thinking_llm=llm, quick_thinking_llm=llm, mandate_context="CTX",
                               conditional_logic=SimpleNamespace())

    v = va.save(config, knobs.compose("entry", {"scale": "entry", "hold_bar": "probability"}))
    rid = replay.start(config, v, "pm", replay.all_cases(config))
    replay.execute(config, rid, graph_factory=factory)
    assert all(node == "Portfolio Manager" for node, _ in seen) and len(seen) >= 2
    assert all("new capital today" in text and "Probability of beating the benchmark" in text for _, text in seen)
    m = replay.metrics(config, rid, panel=None)
    assert m["done"] == 2 and m["ratings"]["Overweight"] == 2 and m["moves"] == {"Hold": {"Overweight": 2}}
    assert [d["probability"] for d in replay.decisions(config, rid)] == [0.61, 0.61]
    # Resumable: a second execute does nothing more.
    replay.execute(config, rid, graph_factory=factory)
    assert replay.metrics(config, rid, panel=None)["done"] == 2


def test_estimates_scale_with_stage_and_models(config):
    base, cheap = va.Variant("baseline"), va.Variant("h", models={"deep": "claude-haiku-4-5", "quick": "claude-haiku-4-5"})
    pm, research = replay.estimate(config, base, "pm", 40), replay.estimate(config, base, "research", 40)
    assert 0 < pm["cost"] < research["cost"]
    assert replay.estimate(config, cheap, "pm", 40)["cost"] < pm["cost"]
    with pytest.raises(ValueError):
        replay.estimate(config, base, "analysts", 1)


def test_desk_saves_knobs_and_guards_the_replay_spend(config, monkeypatch):
    from starlette.testclient import TestClient

    from tradingagents.desk.app import create_app
    from tradingagents.ops import jobs

    started = []
    monkeypatch.setattr(jobs, "start", lambda cfg, kind, args, **kw: started.append((args, kw)) or f"Started job x: {kind}")
    _saved_case(config, ticker="AAA")
    c = TestClient(create_app(config, token="t"), base_url="http://localhost:8810", client=("127.0.0.1", 1))
    h = {"X-Desk-Token": "t", "Origin": "http://localhost:8810"}
    assert [k["key"] for k in c.get("/api/v1/agents/knobs").json()["knobs"]][0] == "scale"
    body = {"name": "entry", "choices": {"scale": "entry"}, "models": {"deep": "claude-haiku-4-5"}}
    assert c.post("/api/v1/agents/knobs", json=body).status_code == 403
    saved = c.post("/api/v1/agents/knobs", json=body, headers=h).json()
    assert saved["knobs"] == {"scale": "entry"} and saved["version"] == 1
    assert c.get("/api/v1/agents/replay/cases").json()["total"] == 1

    req = {"variant": "entry", "stage": "pm", "mandate": "any", "count": 5, "seed": 7, "horizon": 5}
    est = c.post("/api/v1/agents/replay/estimate", json=req).json()
    assert est["cases"] == 1 and est["cost"] > 0
    assert c.post("/api/v1/agents/replays", json=req, headers=h).status_code == 400          # not confirmed
    ok = c.post("/api/v1/agents/replays", json={**req, "confirm_cost": est["cost"]}, headers=h).json()
    args, kw = started[-1]
    assert args == ["agents", "replay-execute", ok["replay"]] and kw["busy"]() is None
    assert c.get("/api/v1/agents/replays").json()["replays"][0]["variant"] == "entry"
    assert "Hold habit" in c.get("/api/v1/agents/playbook").json()["text"]
    assert c.post("/api/v1/agents/replay/estimate", json={**req, "stage": "nope"}).status_code == 400
