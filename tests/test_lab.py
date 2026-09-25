"""The screen lab: panel, replay, statistics, verdict."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tradingagents.lab import panel as lp, replay as rp, report as lr


def _panel(n_names=60, days=420, seed=1, drift_scale=0.004, noise=0.01):
    """Names with a persistent drift each: past winners keep winning, so a
    momentum ordering must pick better than the pool and a random one must not."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2015-01-01", periods=days)
    names = [f"N{j:02d}" for j in range(n_names)] + ["SPY", "RSP"]
    drift = np.r_[rng.normal(0, drift_scale, n_names), 0.0003, 0.0003]
    rets = drift + rng.normal(0, noise, (days, len(names)))
    close = pd.DataFrame(50 * np.exp(np.cumsum(rets, axis=0)), index=dates, columns=names)
    opn = close.shift(1).fillna(close.iloc[0])
    vol = pd.DataFrame(1e6, index=dates, columns=names)
    return lp.Panel(opn.astype(np.float32), close.astype(np.float32), close.astype(np.float32),
                    vol.astype(np.float32))


def _unis(p):
    return {"2014-12-01": [s for s in p.symbols if s not in ("SPY", "RSP")]}


STD = rp.Strategy("t", 5, "SPY", "week", ("momentum_21d", "random"), picks=(8,), min_dollar_volume=(5e6,))


def test_momentum_ordering_beats_pool_and_random_does_not():
    p = _panel()
    ctx = rp.Context(p, _unis(p))
    dates = ctx.schedule("week", "2015-01-01")
    mom = rp.stats(rp.replay(ctx, STD, rp.Variant("momentum_21d", 8, 5e6), dates), STD)
    rnd = rp.stats(rp.replay(ctx, STD, rp.Variant("random", 8, 5e6), dates), STD)
    assert mom.n > 20
    assert mom.selection > 0.01 and mom.t > 3
    assert abs(rnd.selection) < abs(mom.selection) / 3


def test_eligibility_uses_the_traded_price_and_the_listing():
    p = _panel(n_names=12)
    # N00: adjusted close under $5, traded close over it -- eligible.
    p.close["N00"] = p.close["N00"] / 20
    p.open["N00"] = p.open["N00"] / 20
    # N01: traded under $5 -- never eligible.
    p.raw_close["N01"] = 3.0
    unis = {"2014-12-01": [s for s in p.symbols if s not in ("SPY", "RSP", "N02")]}
    ctx = rp.Context(p, unis)
    v = rp.Variant("liquidity", 2, 1e6)
    rows = rp.replay(ctx, STD, v, ctx.schedule("week", "2015-01-01"))
    seen = {s for r in rows for s in r.chosen}
    assert rows and all(r.eligible == 10 for r in rows)  # 12 - N01 - N02
    assert "N01" not in seen and "N02" not in seen


def test_a_name_that_stops_trading_exits_at_its_last_close():
    p = _panel(n_names=4, days=300)
    last = p.close.index[270]
    p.open.loc[p.open.index > last, "N00"] = np.nan
    p.close.loc[p.close.index > last, "N00"] = np.nan
    ctx = rp.Context(p, _unis(p))
    i = 268
    j = ctx.col["N00"]
    got = ctx.forward(i, 5, np.array([j]))[0]
    assert got == pytest.approx(p.close["N00"].iloc[270] / p.open["N00"].iloc[269] - 1, rel=1e-5)


def test_newey_west_widens_for_overlap_and_hurdle_rises_with_trials():
    rng = np.random.default_rng(0)
    base = rng.normal(0.01, 0.05, 400)
    overlapped = pd.Series(base).rolling(6).mean().dropna().to_numpy()
    assert rp.newey_west_t(overlapped, 5) < rp.newey_west_t(overlapped, 0)
    assert rp.t_hurdle(1) == pytest.approx(1.96, abs=0.01)
    assert rp.t_hurdle(40) > rp.t_hurdle(10) > rp.t_hurdle(1)


def _result(signal, tune_t, test_sel, test_t, net=0.001):
    s = rp.Stats(100, 0.01, tune_t, 0.6, 0.0, 1.0, 0.0, 0.0, 0.0, independent=100)
    t = rp.Stats(50, test_sel, test_t, 0.6, net, 1.0, 0.0, 0.0, 0.0, independent=50)
    return rp.Result(rp.Variant(signal, 8, 5e6), s, t)


def test_verdict_chooses_on_tuning_dates_and_confirms_on_test():
    spec = STD
    # The test dates would favour b, but the choice is made on tuning dates.
    results = [_result("a", 4.0, 0.004, 2.5), _result("b", 1.0, 0.02, 5.0), _result("random", 9.0, 0.0, 0.0)]
    v = lr.verdict(spec, results, trials=10)
    assert v.candidate == "a/p8/dv5m" and v.passes and v.tradeable
    v = lr.verdict(spec, [_result("a", 2.5, 0.004, 2.5)], trials=40)
    assert not v.passes and "under" in v.reason
    v = lr.verdict(spec, [_result("a", 4.0, -0.001, 2.5)], trials=10)
    assert not v.passes and "not the test dates" in v.reason


def test_trials_are_counted_per_strategy(tmp_path):
    config = {"data_cache_dir": str(tmp_path)}
    results = [_result("a", 1, 0, 0), _result("b", 1, 0, 0)]
    assert lr.record_trials(config, STD, results) == 2
    assert lr.record_trials(config, STD, results[:1]) == 2
    other = rp.Strategy("u", 5, "SPY", "week", ("a",))
    assert lr.record_trials(config, other, results[:1]) == 1


def _body(prices):
    rows = ["timestamp,open,high,low,close,adjusted_close,volume,dividend_amount,split_coefficient"]
    for d, c in sorted(prices.items(), reverse=True):
        rows.append(f"{d},{c},{c},{c},{c},{c / 2},1000,0.0,1.0")
    return "\n".join(rows)


def test_build_panel_reads_adjusted_and_traded_prices(tmp_path):
    config = {"data_cache_dir": str(tmp_path)}
    bodies = {"SPY": _body({"2015-01-02": 100, "2015-01-05": 101}),
              "AAA": _body({"2015-01-05": 20}),
              "GONE": '{"Error Message": "Invalid API call"}'}
    p = lp.build_panel(config, bodies, start="2015-01-01", bodies=bodies.get)
    assert p.symbols == ["AAA", "SPY"]
    assert math.isnan(p.close.loc["2015-01-02", "AAA"])
    assert p.close.loc["2015-01-05", "AAA"] == 10 and p.raw_close.loc["2015-01-05", "AAA"] == 20


def test_fetch_missing_remembers_absent_symbols_and_keeps_to_budget(tmp_path, monkeypatch):
    config = {"data_cache_dir": str(tmp_path)}
    monkeypatch.setattr(lp, "stored_symbols", lambda c: {"HAVE"})
    asked = []

    def fetch(s):
        asked.append(s)
        return pd.DataFrame() if s == "GONE" else pd.DataFrame({"Close": [1.0]})

    r = lp.fetch_missing(config, ["HAVE", "GONE", "A", "B"], max_requests=2, fetch=fetch)
    assert asked == ["A", "B"] and r.stopped.startswith("budget")
    asked.clear()
    r = lp.fetch_missing(config, ["HAVE", "GONE", "A", "B"], max_requests=10, fetch=fetch)
    assert "GONE" in asked
    asked.clear()
    lp.fetch_missing(config, ["GONE"], max_requests=10, fetch=fetch)
    assert asked == []


def test_a_passing_suggestion_stays_open_until_adopted(tmp_path):
    from tradingagents.lab import adopted

    config = {"data_cache_dir": str(tmp_path)}
    spec = rp.STRATEGIES["standard"]
    assert "No open suggestions" in lr.render_suggestions(config)
    v = lr.verdict(spec, [_result("reversal_5d", 9.0, 0.004, 3.0)], trials=10)
    lr.save_suggestion(config, v)
    assert "lab adopt standard reversal_5d/p8/dv5m" in lr.render_suggestions(config)
    adopted.adopt(config, "standard", "reversal_5d/p8/dv5m")
    assert lr.open_suggestions(config) == []


def test_a_pool_ranks_only_within_the_most_liquid_names():
    p = _panel(n_names=12)
    for k, s in enumerate([f"N{j:02d}" for j in range(12)]):
        p.volume[s] = 1e6 * 8.0 ** k  # N11 most liquid, whatever the prices
    ctx = rp.Context(p, _unis(p))
    rows = rp.replay(ctx, STD, rp.Variant("random", 2, 1e6, pool=4), ctx.schedule("week", "2015-01-01"))
    assert rows and all(r.eligible == 4 for r in rows)
    assert {s for r in rows for s in r.chosen} <= {"N08", "N09", "N10", "N11"}
    assert rp.Variant("random", 2, 1e6, pool=4).id.endswith("/top4")


VAL = rp.Strategy("v", 5, "SPY", "week", ("fcf_pct",), picks=(2,), min_dollar_volume=(1e6,),
                  pools=(None,), fundamentals=True, qualities=(True, False))


def _facts_for(ctx, dates, value, tripped=(), error=()):
    from tradingagents.lab.fundamentals import Facts

    out = {}
    for i in dates:
        day = ctx.dates[i].strftime("%Y-%m-%d")
        for s in ctx.symbols:
            s = str(s)
            if s in ("SPY", "RSP"):
                continue
            out[(s, day)] = Facts(tripped=["debt"] if s in tripped else [], values={"fcf_pct": value(s)},
                                  years=8, error="fundamentals unavailable" if s in error else "")
    return out


def test_value_ranks_on_facts_and_applies_the_quality_exclusions():
    p = _panel(n_names=8)
    ctx = rp.Context(p, _unis(p))
    dates = ctx.schedule("week", "2015-01-01")
    ctx.set_facts(_facts_for(ctx, dates, lambda s: int(s[1:]), tripped={"N07"}, error={"N06"}))
    with_q = rp.replay(ctx, VAL, rp.Variant("fcf_pct", 2, 1e6), dates)
    assert with_q and all(r.chosen == ["N05", "N04"] and r.eligible == 6 for r in with_q)
    no_q = rp.replay(ctx, VAL, rp.Variant("fcf_pct", 2, 1e6, quality=False), dates)
    assert all(r.chosen == ["N07", "N05"] and r.eligible == 7 for r in no_q)
    assert rp.Variant("fcf_pct", 2, 1e6, quality=False).id.endswith("/noq")
    assert len(rp.variants(VAL)) == 2


def test_a_verdict_needs_enough_independent_holdings():
    spec = rp.STRATEGIES["value"]
    r = _result("fcf_pct", 9.0, 0.05, 5.0)
    r.test.independent = 2
    v = lr.verdict(spec, [r], trials=10)
    assert not v.passes and "too few independent holdings" in v.reason


def test_facts_are_filled_once_by_symbol_and_vendor_errors_retried(tmp_path):
    from tradingagents.lab import fundamentals as fu

    config = {"data_cache_dir": str(tmp_path)}
    calls, b_tries = [], []

    def compute(symbol, day):
        calls.append((symbol, day))
        if symbol == "B":
            b_tries.append(1)
            if len(b_tries) == 1:  # the vendor fails the first time only
                return fu.Facts(error="fundamentals unavailable (Invalid API call)", vendor=True)
        return fu.Facts()

    pairs = [("B", "2015-01-02"), ("A", "2015-01-02"), ("A", "2015-02-02"), ("A", "2015-01-02")]
    r = fu.fill(config, pairs, compute_one=compute)
    assert calls == [("A", "2015-01-02"), ("A", "2015-02-02"), ("B", "2015-01-02")]
    assert r.computed == 3 and r.vendor_errors == 1
    calls.clear()
    fu.fill(config, pairs, compute_one=compute)
    assert calls == [("B", "2015-01-02")]  # only the vendor error is asked again
    c = fu.coverage(fu.load_facts(config), pairs)
    assert c["judged"] == 3 and c["vendor_errors"] == 0
