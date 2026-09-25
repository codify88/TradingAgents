"""The benchmark ladder: each rung isolates one source of return."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.evaluation import attribution as at
from tradingagents.screener import manifest as mf


def _line(end_value, days=40, start="2024-01-01"):
    """A price path from 100 to ``end_value`` over ``days`` business days."""
    idx = pd.bdate_range(start, periods=days)
    return pd.Series([100 + (end_value - 100) * i / (days - 1) for i in range(days)], index=idx)


@pytest.fixture
def config(tmp_path):
    return {"results_dir": str(tmp_path), "memory_log_path": str(tmp_path / "live.md")}


def _screen(config, mandate="equity_momentum", as_of="2024-01-01"):
    m = mf.ScreenManifest(run_id=f"{as_of}_{mandate}_000001", mandate=mandate, as_of=as_of,
                          created="2024-01-01T10:00:00", universe_size=4, tiers=[], ordering_signal="x",
                          picks=[{"symbol": "P1", "rank": 1, "value": 1}, {"symbol": "P2", "rank": 2, "value": 1}],
                          controls=[{"symbol": "C1", "value": 0}], control_seed=1, eligible_count=4)
    mf.save_manifest(m, config)
    return m


@pytest.mark.unit
def test_the_ladder_adds_up_and_each_rung_is_right(config, monkeypatch):
    import dataclasses

    from tradingagents.mandates import registry

    quick = dataclasses.replace(registry.get_mandate("equity_momentum"), horizon_days=21, review_horizons_days=())
    monkeypatch.setitem(registry._MANDATES, "equity_momentum", quick)
    _screen(config)
    paths = {"SPY": _line(110), "MTUM": _line(115), "RSP": _line(105),
             "P1": _line(130), "P2": _line(120), "C1": _line(112)}
    prices = lambda s: paths.get(s, pd.Series(dtype=float))  # noqa: E731
    (r,) = at.ladders(config, prices=prices)
    step = 21 / 39
    spy, mtum, c1 = 0.10 * step, 0.15 * step, 0.12 * step
    picks = (0.30 * step + 0.20 * step) / 2
    assert r.horizon == 21 and r.style_symbol == "MTUM" and r.complete
    assert r.style_term == pytest.approx(mtum - spy)
    assert r.universe_term == pytest.approx(c1 - mtum)
    assert r.selection == pytest.approx(picks - c1)
    assert r.style_term + r.universe_term + r.selection == pytest.approx(r.picks - r.market)


@pytest.mark.unit
def test_an_unfinished_horizon_leaves_the_screen_incomplete(config):
    _screen(config, mandate="equity_value")                  # 756 days: nowhere near traded
    (r,) = at.ladders(config, prices=lambda s: _line(110))
    assert not r.complete and math.isnan(r.selection)
    assert "No screen's horizon has traded yet" in at.render([r])


@pytest.mark.unit
def test_the_agent_rung_compares_bullish_ratings_with_the_rest(config, monkeypatch):
    import dataclasses

    from tradingagents.mandates import registry

    quick = dataclasses.replace(registry.get_mandate("equity_momentum"), horizon_days=21, review_horizons_days=())
    monkeypatch.setitem(registry._MANDATES, "equity_momentum", quick)
    m = _screen(config)
    log = TradingMemoryLog({"memory_log_path": config["results_dir"] + "/backtest/s/trading_memory.md"})
    for sym, rating in (("P1", "Buy"), ("P2", "Hold"), ("C1", "Hold")):
        log.store_decision(sym, m.as_of, f"**Rating**: {rating}", mandate="equity_momentum")
    paths = {"SPY": _line(110), "MTUM": _line(110), "RSP": _line(110),
             "P1": _line(100), "P2": _line(150), "C1": _line(150)}
    (r,) = at.ladders(config, prices=lambda s: paths.get(s, pd.Series(dtype=float)))
    assert r.n_bullish == 1 and r.n_other == 2
    assert r.agents < 0, "the agents' Buy did worse than what they passed over"


@pytest.mark.unit
def test_no_mandate_is_graded_on_five_days(config):
    m = mf.ScreenManifest(run_id="2024-01-01_standard_000001", mandate="", as_of="2024-01-01",
                          created="2024-01-01T10:00:00", universe_size=2, tiers=[], ordering_signal="x",
                          picks=[{"symbol": "P1", "rank": 1, "value": 1}], controls=[{"symbol": "C1", "value": 0}],
                          control_seed=1, eligible_count=2)
    mf.save_manifest(m, config)
    (r,) = at.ladders(config, prices=lambda s: _line(110))
    assert r.horizon == 5 and r.style_symbol == "SPY"
