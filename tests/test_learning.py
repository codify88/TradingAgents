"""Lessons, playbooks and the evidence gate: learning that is checked."""
from __future__ import annotations

import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.learning import distill as dl, gate, playbook
from tradingagents.learning.lessons import Lesson, load_lessons


@pytest.fixture
def config(tmp_path):
    return {"results_dir": str(tmp_path), "memory_log_path": str(tmp_path / "live.md"),
            "llm_provider": "anthropic", "deep_think_llm": "x"}


def _log(config, run, rows):
    path = f"{config['results_dir']}/backtest/{run}/trading_memory.md"
    import pathlib

    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    sep = TradingMemoryLog._SEPARATOR
    blocks = []
    for ticker, day, rating, alpha, resolved, pending, reflection, mandate in rows:
        tag = (f"[{day} | {ticker} | {rating} | +0.0% | {alpha} | 756d | resolved:{resolved} | mandate:{mandate}]"
               if not pending else f"[{day} | {ticker} | {rating} | pending | mandate:{mandate}]")
        blocks.append(f"{tag}\n\nDECISION:\nRating: {rating}" + (f"\n\nREFLECTION:\n{reflection}" if reflection else ""))
    pathlib.Path(path).write_text(sep.join(blocks) + sep)


def _lesson(i, ticker, day="2020-01-01", final=True, known="2023-01-01"):
    return Lesson(f"{ticker}|{day}|equity_value|756d#{i}", "equity_value", ticker, day, "Buy", 756, "+10%",
                  known, final, "lesson")


# --- lessons ---------------------------------------------------------------------------


@pytest.mark.unit
def test_lessons_exist_only_from_the_day_their_outcome_was_known(config):
    _log(config, "s1", [
        ("PYPL", "2019-09-03", "Hold", "-42.5%", "2022-09-01", False, "Paid too much for growth.", "equity_value"),
        ("KO", "2020-09-01", "Buy", "+5.0%", "2023-09-01", False, "Moat held.", "equity_value"),
        ("NVDA", "2026-09-17", "Buy", "", "", True, "", "equity_momentum"),
    ])
    lessons = load_lessons(config, mandate="equity_value")
    assert [x.ticker for x in lessons] == ["PYPL", "KO"]
    assert [x.ticker for x in load_lessons(config, mandate="equity_value", known_by="2023-01-01")] == ["PYPL"]
    assert lessons[0].id == "PYPL|2019-09-03|equity_value|756d" and lessons[0].known_on == "2022-09-01"


# --- the gate ----------------------------------------------------------------------------


@pytest.mark.unit
def test_the_gate_wants_breadth_not_one_story_told_five_times():
    one_name = {x.id: x for x in (_lesson(i, "KO", day=f"20{10 + i}-01-01") for i in range(6))}
    ok, why = gate.assess(playbook.Rule("r1", "t", "w", list(one_name)), one_name)
    assert not ok and "names" in why
    broad = {x.id: x for x in (_lesson(i, t, day=d) for i, (t, d) in enumerate(
        [("A", "2019-01-01"), ("B", "2020-01-01"), ("C", "2021-01-01"), ("D", "2019-01-01"), ("E", "2020-01-01")]))}
    assert gate.assess(playbook.Rule("r1", "t", "w", list(broad)), broad)[0]


@pytest.mark.unit
def test_interim_reviews_do_not_count_toward_the_bar():
    interim = {x.id: x for x in (_lesson(i, t, day=f"20{15 + i}-01-01", final=False) for i, t in enumerate("ABCDEF"))}
    ok, why = gate.assess(playbook.Rule("r1", "t", "w", list(interim)), interim)
    assert not ok and "0 settled" in why


@pytest.mark.unit
def test_too_much_contradiction_fails():
    lessons = {x.id: x for x in (_lesson(i, t, day=f"20{15 + i}-01-01") for i, t in enumerate("ABCDEFGHIJ"))}
    ids = list(lessons)
    ok, why = gate.assess(playbook.Rule("r1", "t", "w", ids[:5], ids[5:9]), lessons)
    assert not ok and "contradict" in why


# --- the deterministic check -----------------------------------------------------------------


def _edit(**kw):
    base = {"action": "add", "text": "Discount turnaround narratives until margins actually recover.",
            "why": "Management guidance on recoveries is systematically optimistic.",
            "supports": ["L1"], "contradicts": []}
    return dl.RuleEdit(**{**base, **kw})


@pytest.mark.unit
def test_the_check_drops_what_breaks_the_rules():
    lessons = {"L1": _lesson(1, "PYPL"), "L2": _lesson(2, "KO")}
    assert dl.check(_edit(), lessons, {})[0] is not None
    assert "ticker" in dl.check(_edit(text="Avoid PYPL-style growth premiums."), lessons, {})[1]
    assert "date" in dl.check(_edit(why="As seen in 2022."), lessons, {})[1]
    assert "supports" in dl.check(_edit(supports=["NOPE"]), lessons, {})[1]
    pinned = {"r1": playbook.Rule("r1", "t", "w", state="pinned")}
    assert "pinned" in dl.check(_edit(action="archive", rule_id="r1"), lessons, pinned)[1]
    kept, _ = dl.check(_edit(supports=["L1", "NOPE", "L1"], contradicts=["L1", "L2"]), lessons, {})
    assert kept.supports == ["L1"] and kept.contradicts == ["L2"], "unknown ids dropped, no id on both sides"


@pytest.mark.unit
def test_the_same_lesson_twice_is_one_rule():
    lessons = {x.id: x for x in (_lesson(i, t) for i, t in enumerate("AB"))}
    ids = list(lessons)
    first, _ = dl.apply_edits(None, [_edit(supports=[ids[0]])], lessons, "equity_value", "2026-09-25")
    second, _ = dl.apply_edits(first, [_edit(text="discount turnaround narratives until margins actually recover",
                                             supports=[ids[1]])], lessons, "equity_value", "2026-09-26")
    assert len(second.rules) == 1 and second.rules[0].supports == ids
    assert second.version == 2 and second.available_at == "2023-01-01"


# --- a whole distill, with a fake model ------------------------------------------------------------


class _FakeLLM:
    def __init__(self, proposal):
        self.proposal, self.prompts = proposal, []

    def with_structured_output(self, schema):
        assert schema is dl.Proposal
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.proposal


@pytest.mark.unit
def test_a_distill_saves_a_new_version_and_never_overwrites(config):
    rows = [(t, f"20{19 + i % 3}-03-01", "Buy", "+20.0%", f"20{22 + i % 3}-03-01", False,
             f"Quality compounder rewarded patience ({t}).", "equity_value")
            for i, t in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"])]
    _log(config, "s1", rows)
    ids = [x.id for x in load_lessons(config, mandate="equity_value")]
    llm = _FakeLLM(dl.Proposal(edits=[
        _edit(text="Hold durable compounders through drawdowns.", why="Earning power, not price, sets value.",
              supports=ids),
        _edit(text="Buy AAA again.", supports=ids[:1]),   # names a ticker: dropped
    ]))
    result = dl.distill(config, "equity_value", llm=llm, today="2026-09-25")
    assert len(result.accepted) == 1 and len(result.rejected) == 1
    pb = playbook.latest(config, "equity_value")
    assert pb.version == 1 and pb.rules[0].state == "eligible"
    assert "No tickers" in llm.prompts[0] and ids[0] in llm.prompts[0]
    with pytest.raises(FileExistsError):
        playbook.save(config, pb)


@pytest.mark.unit
def test_a_historical_run_loads_only_a_version_that_existed_then(config):
    for v, avail in ((1, "2023-01-01"), (2, "2025-06-01")):
        playbook.save(config, playbook.Playbook("equity_value", v, "2026-09-25", avail, []))
    assert playbook.latest(config, "equity_value").version == 2
    assert playbook.latest(config, "equity_value", available_by="2024-01-01").version == 1
    assert playbook.latest(config, "equity_value", available_by="2022-01-01") is None
