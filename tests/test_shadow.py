"""Shadow picks: recorded from the last close with the lab's own selection, scored later."""

from __future__ import annotations

import pytest

from tests.test_lab import _panel, _unis
from tradingagents.lab import panel as lp, replay as rp, shadow


def _cut(p, n):
    """The panel as it stood n days in: the night the record is made."""
    return lp.Panel(p.open.iloc[:n], p.close.iloc[:n], p.raw_close.iloc[:n], p.volume.iloc[:n])


@pytest.fixture
def config(tmp_path):
    return {"data_cache_dir": str(tmp_path)}


def test_a_night_records_the_candidate_and_the_live_ordering_once(config, monkeypatch):
    full = _panel(n_names=30, days=420)
    night = rp.Context(_cut(full, 400), _unis(full))
    monkeypatch.setattr(rp.Context, "holds", lambda self, condition, i: True)  # a stressed night
    got = shadow.record(config, night)
    by = {r["variant"]: r for r in got}
    cand = by["liquidity/p8/dv5m/if_stressed:reversal_5d"]
    live = by["liquidity/p8/dv5m"]
    assert cand["date"] == live["date"] == full.dates[399].strftime("%Y-%m-%d")
    assert cand["condition"] is True and cand["signal"] == "reversal_5d" and len(cand["picks"]) == 8
    assert live["condition"] is None and live["signal"] == "liquidity"
    # The last close has no next open yet, and is still recorded.
    eligible = night.liquid_eligible(399, 5e6, None, need_next_open=False).size
    assert not set(cand["picks"]) & set(cand["pool"]) and len(cand["picks"]) + len(cand["pool"]) == eligible
    assert night.liquid_eligible(399, 5e6, None).size == 0  # the backtest's rule would record nothing
    # The same close read again (a weekend night) records nothing new.
    assert shadow.record(config, night) == [] and len(shadow.records(config)) == 2


def test_it_picks_what_the_lab_would_have_picked_that_day(config, monkeypatch):
    full = _panel(n_names=30, days=420)
    monkeypatch.setattr(rp.Context, "holds", lambda self, condition, i: True)
    night = rp.Context(_cut(full, 400), _unis(full))
    rec = {r["variant"]: r for r in shadow.record(config, night)}
    later = rp.Context(full, _unis(full))
    spec = rp.STRATEGIES["standard"]
    v = next(x for x in rp.variants(spec) if x.id == "liquidity/p8/dv5m/if_stressed:reversal_5d")
    sel = rp.select(later, spec, v, 399)
    assert rec[v.id]["picks"] == [str(s) for s in later.symbols[sel.chosen]]


def test_records_are_pending_until_their_week_has_traded(config, monkeypatch):
    full = _panel(n_names=30, days=420)
    monkeypatch.setattr(rp.Context, "holds", lambda self, condition, i: True)
    shadow.record(config, rp.Context(_cut(full, 400), _unis(full)))
    early = shadow.score(config, rp.Context(_cut(full, 403), _unis(full)))
    assert all(s.n == 0 and s.pending == 1 for s in early)
    done = {s.variant: s for s in shadow.score(config, rp.Context(full, _unis(full)))}
    cand = done["liquidity/p8/dv5m/if_stressed:reversal_5d"]
    assert cand.n == 1 and cand.pending == 0 and cand.condition_days == 1
    assert cand.selection == pytest.approx(cand.selection_when_held)
    text = shadow.render(list(done.values()), [])
    assert "never traded" in text and "liquidity/p8/dv5m" in text


def test_a_thin_last_day_is_not_recorded_and_its_breadth_is_withheld(config):
    import numpy as np

    from tradingagents.lab import regime

    full = _panel(n_names=30, days=420)
    thin = _cut(full, 400)
    for f in (thin.close, thin.open, thin.raw_close, thin.volume):
        f.iloc[-1, :25] = np.nan  # only five histories refreshed after the last close
    assert lp.last_day_thin(thin) and lp.last_day_thin(_cut(full, 400)) is None
    with pytest.raises(shadow.ThinDay, match="only"):
        shadow.record(config, rp.Context(thin, _unis(full)))
    assert shadow.records(config) == []
    b = regime.breadth(thin, 50)
    assert np.isnan(b.iloc[-1]) and not np.isnan(b.iloc[-2])
