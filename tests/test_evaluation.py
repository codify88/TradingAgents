"""The evaluation harness: one registry, one comparison, one bar, one queue."""
from __future__ import annotations

import dataclasses

import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.evaluation import bar, compare, queue, trials
from tradingagents.mandates import registry
from tradingagents.mandates.registry import get_mandate, serves
from tradingagents.screener import manifest as mf, run as sr


@pytest.fixture
def config(tmp_path, monkeypatch):
    lenses = dataclasses.replace(get_mandate("equity_value"), name="equity_value_trial",
                                 label="trial", base="equity_value", scores_as_base=False)
    sloppy = dataclasses.replace(get_mandate("equity_value"), name="equity_value_sloppy",
                                 label="sloppy", base="equity_value")
    monkeypatch.setitem(registry._MANDATES, lenses.name, lenses)
    monkeypatch.setitem(registry._MANDATES, sloppy.name, sloppy)
    return {"results_dir": str(tmp_path), "memory_log_path": str(tmp_path / "live.md"),
            "data_vendors": {"core_stock_apis": "yfinance"}}


def _screen(config, n, as_of, picks=("P1", "P2", "P3", "P4", "P5"), controls=("C1", "C2", "C3", "C4", "C5")):
    m = mf.ScreenManifest(
        run_id=f"{as_of}_equity_value_{n:06d}", mandate="equity_value", as_of=as_of,
        created=f"2026-09-18T10:{n:02d}:00", universe_size=10, tiers=[], ordering_signal="x",
        picks=[{"symbol": s, "rank": i + 1, "value": 0.1} for i, s in enumerate(picks)],
        controls=[{"symbol": s, "value": 0.0} for s in controls], control_seed=1, eligible_count=10)
    mf.save_manifest(m, config)
    return m


def _settle(config, m, mandate, pick_alpha, control_alpha):
    """Log settled decisions for every name of screen ``m`` under ``mandate``."""
    path = sr.sweep_log_path(m, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for sym, a in [(s, pick_alpha) for s in m.pick_symbols] + [(s, control_alpha) for s in m.control_symbols]:
        blocks.append(f"[{m.as_of} | {sym} | Hold | +0.0% | {a:+.1%} | 756d | resolved:2026-01-01 | "
                      f"mandate:{mandate}]\n\nDECISION:\nRating: Hold\n\nREFLECTION:\nok")
    existing = path.read_text() if path.exists() else ""
    sep = TradingMemoryLog._SEPARATOR
    path.write_text(existing + sep.join(blocks) + sep)


# --- a trial overlay never stands in for its base ----------------------------------------


@pytest.mark.unit
def test_a_trial_overlays_decisions_do_not_count_for_the_base(config):
    assert not serves("equity_value_trial", "equity_value")
    assert serves("equity_momentum_leaps", "equity_momentum"), "LEAPS still does: it only adds a choice"


# --- the bar -------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_bar_rises_with_the_trials_tried_in_a_style():
    assert bar.required_screens(1) == 4
    assert bar.required_screens(5) == 4
    assert bar.required_screens(6) == 5
    assert bar.required_screens(11) == 6


def _cmp(base, cand, settled=(5, 5)):
    return bar.ScreenComparison("s", "2020-01-01", base, cand, settled, settled)


@pytest.mark.unit
def test_verdicts():
    assert bar.verdict([_cmp(0.0, 0.1)] * 3, 1).outcome == "not enough settled"
    assert bar.verdict([_cmp(0.0, 0.1)] * 3 + [_cmp(0.1, 0.0)], 1).outcome == "promote"   # 3 of 4
    assert bar.verdict([_cmp(0.0, 0.1)] * 2 + [_cmp(0.1, 0.0)] * 2, 1).outcome == "archive"
    thin = [_cmp(0.0, 0.1, settled=(4, 5))] * 10
    assert bar.verdict(thin, 1).outcome == "not enough settled", "4 settled picks is not enough"


# --- registering a trial -------------------------------------------------------------------------


@pytest.mark.unit
def test_register_refuses_an_unfair_test(config):
    _screen(config, 1, "2021-03-01")
    _screen(config, 2, "2024-03-01")
    ok = {"trial_id": "t-one", "kind": "overlay", "style": "value", "base": "equity_value",
          "candidate": "equity_value_trial", "screens": ["000001", "000002"]}
    with pytest.raises(ValueError, match="scores as its base"):
        trials.register(config, **{**ok, "candidate": "equity_value_sloppy"})
    with pytest.raises(ValueError, match="not after the evidence"):
        trials.register(config, **{**ok, "kind": "playbook", "evidence_through": "2022-01-01"})
    with pytest.raises(ValueError, match="fixed before it runs"):
        trials.register(config, **{**ok, "screens": []})
    t = trials.register(config, **ok)
    assert t.screens == ["2021-03-01_equity_value_000001", "2024-03-01_equity_value_000002"]
    with pytest.raises(ValueError, match="already exists"):
        trials.register(config, **ok)
    assert trials.trials_in_style(config, "value") == 1


# --- comparing arms ------------------------------------------------------------------------------


@pytest.mark.unit
def test_each_arm_is_scored_against_its_own_controls(config):
    screens = [_screen(config, i, f"20{19 + i}-03-01") for i in range(1, 5)]
    t = trials.register(config, trial_id="t-two", kind="overlay", style="value", base="equity_value",
                        candidate="equity_value_trial", screens=[m.run_id for m in screens])
    for i, m in enumerate(screens):
        _settle(config, m, "equity_value", 0.05, 0.02)                       # base edge +3%
        _settle(config, m, "equity_value_trial", 0.08 if i < 3 else 0.0, 0.02)  # +6% on three
    comparisons = compare.compare(t, config)
    assert [round(c.base_edge, 3) for c in comparisons] == [0.03] * 4
    assert [round(c.candidate_edge, 3) for c in comparisons] == [0.06, 0.06, 0.06, -0.02]
    comps, v = compare.evaluate(t, config)
    assert v.outcome == "promote" and v.wins == 3
    saved = trials.get_trial(config, "t-two")
    assert saved.status == "promoted" and saved.verdicts[0]["bar_version"] == bar.BAR_VERSION


@pytest.mark.unit
def test_an_unsettled_trial_is_not_judged_or_changed(config):
    m = _screen(config, 1, "2021-03-01")
    t = trials.register(config, trial_id="t-three", kind="overlay", style="value", base="equity_value",
                        candidate="equity_value_trial", screens=[m.run_id])
    _, v = compare.evaluate(t, config)
    assert v.outcome == "not enough settled"
    assert trials.get_trial(config, "t-three").status == "active"


# --- the nightly queue ---------------------------------------------------------------------------


@pytest.mark.unit
def test_with_no_trials_every_slot_goes_to_the_backlog(config):
    _screen(config, 1, "2021-03-01")
    slots = queue.plan_night(config, "equity_value", max_names=5, backlog=2)
    assert [(s.run_as, s.names) for s in slots] == [(None, 5)]


@pytest.mark.unit
def test_fresh_limits_the_backlog_to_that_days_screens(config):
    _screen(config, 1, "2021-03-01")
    today = _screen(config, 2, "2021-03-02")
    slots = queue.plan_night(config, "equity_value", max_names=5, backlog=5, fresh="2021-03-02")
    assert [s.plan.manifest.run_id for s in slots] == [today.run_id]


@pytest.mark.unit
def test_trials_take_three_and_the_backlog_two(config):
    a = _screen(config, 1, "2021-03-01")
    b = _screen(config, 2, "2022-03-01")
    trials.register(config, trial_id="t-q", kind="overlay", style="value", base="equity_value",
                    candidate="equity_value_trial", screens=[b.run_id])
    slots = queue.plan_night(config, "equity_value", max_names=5, backlog=2)
    by = {(s.plan.manifest.run_id, s.run_as): s.names for s in slots}
    assert by[(b.run_id, "equity_value_trial")] == 3
    assert by[(a.run_id, None)] == 2


@pytest.mark.unit
def test_a_slot_is_never_wasted_and_never_overfilled(config):
    small = _screen(config, 1, "2021-03-01", picks=("P1",), controls=("C1",))  # 2 names only
    trials.register(config, trial_id="t-small", kind="overlay", style="value", base="equity_value",
                    candidate="equity_value_trial", screens=[small.run_id])
    # The backlog is this same screen under the base; both arms have 2 names each.
    slots = queue.plan_night(config, "equity_value", max_names=5, backlog=2)
    assert sum(s.names for s in slots) == 4, "only four names exist to run"
    assert all(s.names <= len(s.plan.todo) for s in slots)


@pytest.mark.unit
def test_two_trials_share_the_trial_slots_round_robin(config):
    m1 = _screen(config, 1, "2021-03-01")
    m2 = _screen(config, 2, "2022-03-01")
    other = dataclasses.replace(get_mandate("equity_value_trial"), name="equity_value_trial2")
    registry._MANDATES[other.name] = other
    try:
        trials.register(config, trial_id="t-a", kind="overlay", style="value", base="equity_value",
                        candidate="equity_value_trial", screens=[m1.run_id])
        trials.register(config, trial_id="t-b", kind="overlay", style="value", base="equity_value",
                        candidate="equity_value_trial2", screens=[m2.run_id])
        slots = queue.plan_night(config, "equity_value", max_names=3, backlog=0)
        per_trial = {s.trial: s.names for s in slots if s.trial}
        assert per_trial == {"t-a": 2, "t-b": 1}
    finally:
        registry._MANDATES.pop(other.name, None)
