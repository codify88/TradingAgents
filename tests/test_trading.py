"""Paper execution: order plans, approval, the kill switch, reconciliation."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from tradingagents.trading import execute as ex, plan as pl
from tradingagents.trading.book import load_book
from tradingagents.trading.broker import Account, BrokerError, Order, Position

NY = pl.NEW_YORK


class FakeBroker:
    """Alpaca as the book sees it: sessions, OPG fills at the next open, and a
    refusal for a client order id it has seen before."""

    paper = True

    def __init__(self, equity=100_000.0):
        self.cash = equity
        self.pos: dict[str, float] = {}
        self.px: dict[str, float] = {}
        self.orders_: dict[str, Order] = {}
        self.refuse: set[str] = set()
        self.cancelled = 0

    def account(self):
        long = sum(q * self.px.get(s, 100.0) for s, q in self.pos.items())
        return Account(self.cash + long, self.cash, self.cash, long, False)

    def positions(self):
        return [Position(s, q, q * self.px.get(s, 100.0)) for s, q in self.pos.items() if q]

    def submit(self, symbol, qty, side, client_order_id):
        if client_order_id in self.orders_:
            raise BrokerError("client_order_id must be unique")
        if symbol in self.refuse:
            raise BrokerError("asset not tradable")
        o = Order(client_order_id, symbol, side, qty, "accepted", 0, None)
        self.orders_[client_order_id] = o
        return o

    def open(self, partial: dict[str, float] | None = None):
        """The opening auction: every accepted order fills (or partly)."""
        for o in self.orders_.values():
            if o.status != "accepted":
                continue
            q = (partial or {}).get(o.symbol, o.qty)
            px = self.px.get(o.symbol, 100.0)
            o.filled_qty, o.filled_avg_price = q, px
            o.status = "filled" if q == o.qty else "canceled"
            sign = 1 if o.side == "buy" else -1
            self.pos[o.symbol] = self.pos.get(o.symbol, 0) + sign * q
            self.cash -= sign * q * px

    def orders(self, after):
        return list(self.orders_.values())

    def cancel_open(self):
        n = sum(o.status == "accepted" for o in self.orders_.values())
        for o in self.orders_.values():
            if o.status == "accepted":
                o.status = "canceled"
        self.cancelled += n
        return n

    def sessions(self, start, end):
        return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)]


@pytest.fixture
def config(tmp_path):
    return {"data_cache_dir": str(tmp_path / "cache"), "results_dir": str(tmp_path / "results"),
            "trading_dir": str(tmp_path / "trading")}


def _at(day, hh=7, mm=0):
    return datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}").replace(tzinfo=NY)


def _plan(config, broker, picks, day="2026-10-05", hh=7, rules=None):
    import unittest.mock as um

    with um.patch.object(pl, "entries", return_value=(picks, ["test"])):
        p = pl.build(config, broker, load_book(config), now=_at(day, hh), price=lambda s: 100.0,
                     rules=rules or pl.Rules())
    pl.save_plan(config, p)
    return p


def test_sizing_is_equal_per_cohort_capped_and_overweight_is_half(config):
    b = FakeBroker()
    p = _plan(config, b, [("AAA", 1.0), ("BBB", 0.5)])
    qty = {o.symbol: o.qty for o in p.buys}
    # 100k / 5 cohorts / 2 names = 10k, capped at 5% = 5k -> 50 shares; half for Overweight.
    assert qty == {"AAA": 50, "BBB": 25}
    assert p.session == "2026-10-05" and p.exit_session == "2026-10-12"
    many = _plan(config, b, [(f"N{i}", 1.0) for i in range(8)])
    assert {o.qty for o in many.buys} == {25}  # 20k cohort / 8 names = 2.5k


def test_after_the_cutoff_the_plan_is_for_the_next_open(config):
    p = _plan(config, FakeBroker(), [("AAA", 1.0)], day="2026-10-05", hh=10)
    assert p.session == "2026-10-06"


def test_submit_places_each_order_once_and_opens_a_cohort(config):
    b = FakeBroker()
    p = _plan(config, b, [("AAA", 1.0), ("BBB", 1.0)])
    msg = ex.submit(config, b, p.id, now=_at("2026-10-05", 8))
    assert "sent 2 of 2" in msg and len(b.orders_) == 2
    assert "submitted" in ex.submit(config, b, p.id, now=_at("2026-10-05", 8))
    assert len(b.orders_) == 2
    (c,) = load_book(config).live
    assert c.planned == {"AAA": 50, "BBB": 50} and c.exit_session == "2026-10-12"


def test_an_expired_plan_sends_nothing(config):
    b = FakeBroker()
    p = _plan(config, b, [("AAA", 1.0)])
    assert "expired" in ex.submit(config, b, p.id, now=_at("2026-10-05", 9, 29))
    assert not b.orders_


def test_halt_refuses_and_cancels_resume_restores(config):
    b = FakeBroker()
    p = _plan(config, b, [("AAA", 1.0)])
    ex.submit(config, b, p.id, now=_at("2026-10-05", 8))
    assert "1 open order(s) cancelled" in ex.halt(config, b, "test")
    q = _plan(config, b, [("CCC", 1.0)], day="2026-10-06")
    assert "halted" in ex.submit(config, b, q.id, now=_at("2026-10-06", 8))
    ex.resume(config)
    assert "sent 1 of 1" in ex.submit(config, b, q.id, now=_at("2026-10-06", 8))


def test_a_full_cycle_enters_reconciles_exits_and_closes(config):
    b = FakeBroker()
    p = _plan(config, b, [("AAA", 1.0), ("BBB", 1.0)])
    ex.submit(config, b, p.id, now=_at("2026-10-05", 8))
    b.open()
    assert ex.reconcile(config, b, now=_at("2026-10-05", 17)) == []
    (c,) = load_book(config).live
    assert c.reconciled and c.held == {"AAA": 50, "BBB": 50}
    # Before the exit session nothing is sold.
    mid = _plan(config, b, [], day="2026-10-08")
    assert not mid.sells
    # On the exit session both are sold; BBB is picked again and carried instead.
    q = _plan(config, b, [("BBB", 1.0), ("CCC", 1.0)], day="2026-10-12")
    assert [(o.symbol, o.side, o.qty) for o in q.sells] == [("AAA", "sell", 50)]
    assert q.carried == {"BBB": 50} and [o.symbol for o in q.buys] == ["CCC"]
    ex.submit(config, b, q.id, now=_at("2026-10-12", 8))
    b.open()
    assert ex.reconcile(config, b, now=_at("2026-10-12", 17)) == []
    book = load_book(config)
    assert [c.plan_id for c in book.live] == [q.id]
    assert book.live[0].shares() == {"BBB": 50, "CCC": 50}
    # Reconciling again books nothing twice.
    assert ex.reconcile(config, b, now=_at("2026-10-13", 17)) == []
    assert load_book(config).live[0].shares() == {"BBB": 50, "CCC": 50}


def test_a_partial_fill_or_a_stray_position_blocks_until_acknowledged(config):
    b = FakeBroker()
    p = _plan(config, b, [("AAA", 1.0)])
    ex.submit(config, b, p.id, now=_at("2026-10-05", 8))
    b.open(partial={"AAA": 20})
    b.pos["ZZZ"] = 5  # bought by hand at the broker
    problems = ex.reconcile(config, b, now=_at("2026-10-05", 17))
    assert any("filled 20 of 50" in x for x in problems)
    assert any(x.startswith("ZZZ") for x in problems)
    assert load_book(config).live[0].shares() == {"AAA": 20}
    q = _plan(config, b, [("CCC", 1.0)], day="2026-10-06")
    assert "mismatches" in ex.submit(config, b, q.id, now=_at("2026-10-06", 8))
    ex.acknowledge(config)
    b.pos.pop("ZZZ")
    assert "sent 1 of 1" in ex.submit(config, b, q.id, now=_at("2026-10-06", 8))


def test_a_refused_order_is_reported_and_blocks(config):
    b = FakeBroker()
    b.refuse.add("BBB")
    p = _plan(config, b, [("AAA", 1.0), ("BBB", 1.0)])
    msg = ex.submit(config, b, p.id, now=_at("2026-10-05", 8))
    assert "sent 1 of 2" in msg and "not tradable" in msg
    assert load_book(config).blocked


def test_live_endpoint_is_refused_without_the_switch(config):
    b = FakeBroker()
    b.paper = False
    p = _plan(config, b, [("AAA", 1.0)])
    assert "live trading is not enabled" in ex.submit(config, b, p.id, now=_at("2026-10-05", 8))
    assert not b.orders_


def test_the_broker_refuses_the_live_url_by_default(monkeypatch):
    from tradingagents.trading.broker import PAPER_URL, AlpacaBroker

    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")

    class S:
        headers: dict = {}

    assert AlpacaBroker({}, session=S()).base == PAPER_URL
    monkeypatch.delenv("ALPACA_API_KEY")
    with pytest.raises(BrokerError):
        AlpacaBroker({}, session=S())


def test_entries_follow_the_agents_or_the_screen(config, tmp_path):
    from tradingagents.screener import manifest as mf

    m = mf.ScreenManifest(
        run_id="2026-10-05_none_020000", mandate="", as_of="2026-10-05", created="now",
        universe_size=10, tiers=[], ordering_signal="x",
        picks=[{"symbol": s, "rank": i + 1, "value": 1.0} for i, s in enumerate(["AAA", "BBB", "CCC"])],
        controls=[{"symbol": "DDD", "value": 0.0}], control_seed=1, eligible_count=4, notes=[])
    mf.save_manifest(m, config)
    got, notes = pl.entries(config, "2026-10-05", pl.Rules(entry="screen"))
    assert [s for s, _ in got] == ["AAA", "BBB", "CCC"]
    got, notes = pl.entries(config, "2026-10-05", pl.Rules())
    assert got == [] and any("not yet decided" in n for n in notes)
