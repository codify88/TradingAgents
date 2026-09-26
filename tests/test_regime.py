"""The market's state: trend and shape from trailing windows, nothing from after the date."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradingagents.lab import regime as rg
from tradingagents.lab.panel import Panel


def _panel(spy: np.ndarray, names: dict[str, np.ndarray] | None = None) -> Panel:
    idx = pd.bdate_range("2015-01-01", periods=len(spy))
    close = pd.DataFrame({"SPY": spy, **(names or {})}, index=idx, dtype="float32")
    return Panel(open=close, close=close, raw_close=close, volume=close * 0 + 1e6)


def _walk(n: int, drift: float = 0.0, vol: float = 0.01, seed: int = 1, start: float = 100.0) -> np.ndarray:
    r = np.random.default_rng(seed).normal(drift, vol, n)
    return start * np.exp(np.cumsum(r))


def _range(n: int, seed: int = 2, level: float = 100.0) -> np.ndarray:
    """Mean-reverting around a level: contained, going nowhere."""
    rng, x, out = np.random.default_rng(seed), 0.0, []
    for _ in range(n):
        x = 0.6 * x + rng.normal(0, 0.01)
        out.append(level * np.exp(x))
    return np.array(out)


@pytest.mark.unit
def test_a_steady_rise_is_an_uptrend_in_an_up_channel():
    s = rg.compute(_panel(_walk(400, drift=0.004, vol=0.006)), "long")
    tail = s.iloc[-60:]
    assert (tail["trend"] == "up").all()
    assert (tail["shape"] == "channel_up").mean() > 0.8
    assert not tail["stressed"].any()


@pytest.mark.unit
def test_a_mean_reverting_market_is_a_range_and_a_fall_is_a_downtrend():
    s = rg.compute(_panel(np.concatenate([_range(400), _walk(150, -0.004, 0.006, start=100.0)])), "long")
    assert (s["shape"].iloc[300:400] == "range").mean() > 0.8
    assert s["trend"].iloc[-1] == "down" and s["stressed"].iloc[-1]
    assert s["shape"].iloc[-1] in ("channel_down", "breakout_down")


@pytest.mark.unit
def test_a_breakout_leaves_a_range_and_is_confirmed_only_by_its_hold():
    p = rg.PROFILES["long"]
    base = 100 + np.sin(np.arange(300) * 2 * np.pi / 7)  # a range between 99 and 101
    jump = 101 * np.exp(np.cumsum(np.full(10, 0.02)))  # ten strong closes above it
    s = rg.compute(_panel(np.concatenate([base, jump])), "long")
    first = 300  # the first close beyond the range
    assert s["shape"].iloc[first - 1] == "range"
    assert not s["shape"].iloc[first:first + p.hold - 1].str.startswith("breakout").any()
    assert s["shape"].iloc[first + p.hold - 1] == "breakout_up"


@pytest.mark.unit
def test_new_highs_inside_an_up_channel_are_not_a_breakout():
    s = rg.compute(_panel(_walk(500, drift=0.004, vol=0.006)), "long")
    assert (s["shape"].iloc[300:] == "breakout_up").mean() < 0.1


@pytest.mark.unit
def test_one_odd_day_does_not_flip_the_label():
    raw = pd.Series(["range"] * 10 + ["channel_up"] + ["range"] * 5)
    labels, days = rg._hysteresis(raw, pd.Series([False] * len(raw)), 3)
    assert set(labels) == {"range"} and days[-1] == len(raw)
    raw = pd.Series(["range"] * 10 + ["channel_up"] * 4)
    labels, _ = rg._hysteresis(raw, pd.Series([False] * len(raw)), 3)
    assert labels[11] == "range" and labels[12] == "channel_up"


@pytest.mark.unit
@pytest.mark.parametrize("profile", ["long", "short"])
def test_a_dates_label_uses_nothing_after_it(profile):
    spy = np.concatenate([_walk(300, 0.002, 0.01, seed=3), _range(150, seed=4, level=180),
                          _walk(150, -0.003, 0.015, seed=5, start=180)])
    full = rg.compute(_panel(spy), profile)
    for cut in (320, 400, 480, 560):
        part = rg.compute(_panel(spy[:cut]), profile)
        cols = ["trend", "shape", "days", "stressed"]
        pd.testing.assert_frame_equal(part[cols], full[cols].iloc[:cut], check_dtype=False)


@pytest.mark.unit
def test_breadth_is_the_share_of_liquid_names_above_their_own_average():
    n = 260
    names = {"UP": _walk(n, 0.003, 0.005, seed=6), "DOWN": _walk(n, -0.003, 0.005, seed=7)}
    p = _panel(_walk(n, seed=8), names)
    b = rg.breadth(p, 50)
    assert b.iloc[-1] == pytest.approx(0.5, abs=0.34)  # SPY itself may be either side
    p.volume.loc[:, "DOWN"] = 0.0  # illiquid: left out
    assert rg.breadth(p, 50).iloc[-1] in (0.5, 1.0)


@pytest.mark.unit
def test_saved_state_is_what_desk_and_today_read(tmp_path):
    config = {"data_cache_dir": str(tmp_path), "results_dir": str(tmp_path / "results")}
    s = rg.compute(_panel(_walk(400, 0.004, 0.006)), "long")
    rg.save(config, "long", s)
    got = rg.load(config, "long")
    assert got["current"]["trend"] == "up" and got["current"]["as_of"] == str(s.index[-1].date())
    assert got["history"][-1]["shape"] == got["current"]["shape"]
    assert rg.load(config, "short") is None

    from tradingagents.desk import today

    market = today._market(config)
    assert market["long"]["for"] == ["momentum", "value"] and "short" not in market
    assert today._market({"data_cache_dir": str(tmp_path / "none")}) is None


# --- market-state variants in the lab ------------------------------------------------


def _ctx(days=520):
    from tests.test_lab import _panel as lab_panel, _unis
    from tradingagents.lab import replay as rp

    p = lab_panel(days=days)
    return rp, rp.Context(p, _unis(p))


@pytest.mark.unit
def test_market_state_variants_are_named_not_a_grid_and_cannot_be_adopted(tmp_path):
    from tradingagents.lab import adopted, replay as rp

    std = [v.id for v in rp.variants(rp.STRATEGIES["standard"])]
    assert len(std) == 34 and sum("/if_" in v for v in std) == 2
    assert "liquidity/p8/dv5m/if_stressed:reversal_5d" in std
    assert "excess_12m/p8/dv5m/top60/if_long_down:cash" in [v.id for v in rp.variants(rp.STRATEGIES["momentum"])]
    assert len(rp.variants(rp.STRATEGIES["value"])) == 48
    with pytest.raises(ValueError, match="market's state"):
        adopted.adopt({"data_cache_dir": str(tmp_path)}, "standard", "liquidity/p8/dv5m/if_stressed:reversal_5d")


@pytest.mark.unit
def test_a_variant_switches_only_on_the_dates_its_condition_held(monkeypatch):
    rp, ctx = _ctx()
    spec = rp.Strategy("t", 5, "SPY", "week", ("liquidity",), picks=(8,), min_dollar_volume=(5e6,))
    dates = ctx.schedule("week", "2015-01-01")
    on = set(dates[::3])
    monkeypatch.setattr(ctx, "holds", lambda condition, i: i in on)
    base = rp.replay(ctx, spec, rp.Variant("liquidity", 8, 0.0), dates)
    alt = rp.replay(ctx, spec, rp.Variant("momentum_21d", 8, 0.0), dates)
    mixed = rp.replay(ctx, spec, rp.Variant("liquidity", 8, 0.0, when="stressed:momentum_21d"), dates)
    assert mixed and len(base) == len(alt) == len(mixed)
    for b, a, m in zip(base, alt, mixed, strict=True):
        i = ctx.dates.get_loc(pd.Timestamp(m.date))
        assert m.chosen == (a.chosen if i in on else b.chosen)


@pytest.mark.unit
def test_cash_earns_nothing_and_keeps_the_whole_pool(monkeypatch):
    rp, ctx = _ctx()
    spec = rp.Strategy("t", 5, "SPY", "week", ("liquidity",), picks=(8,), min_dollar_volume=(5e6,))
    dates = ctx.schedule("week", "2015-01-01")
    monkeypatch.setattr(ctx, "holds", lambda condition, i: True)
    rows = rp.replay(ctx, spec, rp.Variant("liquidity", 8, 0.0, when="long_down:cash"), dates)
    assert rows and all(r.picks == 0.0 and r.chosen == [] for r in rows)
    assert all(r.selection == -r.pool for r in rows)


@pytest.mark.unit
def test_the_condition_reads_the_state_at_the_dates_close():
    rp, ctx = _ctx()
    states = rg.compute(ctx._panel, "long").reindex(ctx.dates)
    for i in (300, 400, 500):
        assert ctx.holds("long_down", i) == (states["trend"].iloc[i] == "down")
        assert ctx.holds("stressed", i) == bool(states["stressed"].iloc[i])
    assert ctx.holds("stressed", 10) is False  # windows not filled yet
