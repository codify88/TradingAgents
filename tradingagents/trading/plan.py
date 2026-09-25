"""The order plan: tomorrow's (or this morning's) entries and exits, before the open.

Entries come from the standard strategy's screen for the decision date: its
picks, kept when the agents rated them Buy (full size) or Overweight (half) --
or every pick, in ``screen`` mode, which trades the screener alone. Exits are
the cohorts whose holding ends at this open.

Sizing (go-live defaults): the account is split across ``cohorts`` live
cohorts, each entry gets an equal share of its cohort's slice, capped at
``max_position`` of equity; long only, never more than the cash on hand, never
more than 100% of equity invested. Orders are whole shares, market-on-open.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .book import Book, trading_dir
from .broker import Account, Broker, Position

NEW_YORK = ZoneInfo("America/New_York")
# Market-on-open orders must reach the broker by 09:28 New York time.
OPG_CUTOFF = time(9, 28)
STRATEGY = "standard"


@dataclass(frozen=True)
class Rules:
    cohorts: int = 5
    hold_sessions: int = 5
    max_position: float = 0.05
    overweight_scale: float = 0.5
    entry: str = "agents"            # agents | screen

    @classmethod
    def from_config(cls, config: dict) -> Rules:
        over = config.get("trading_rules") or {}
        return cls(**{k: v for k, v in over.items() if k in cls.__dataclass_fields__})


@dataclass
class PlannedOrder:
    symbol: str
    side: str            # buy | sell
    qty: int
    reason: str


@dataclass
class OrderPlan:
    id: str
    strategy: str
    decision_date: str
    session: str
    expires: str                       # ISO, New York time
    created: str
    equity: float
    orders: list[PlannedOrder] = field(default_factory=list)
    exit_cohorts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    status: str = "pending"            # pending | submitted | expired | refused
    submitted: str = ""
    submit_errors: list[str] = field(default_factory=list)
    exit_session: str = ""
    # Shares an exiting cohort already holds in a name the new cohort buys:
    # kept rather than sold and bought back at the same open.
    carried: dict[str, int] = field(default_factory=dict)

    @property
    def buys(self) -> list[PlannedOrder]:
        return [o for o in self.orders if o.side == "buy"]

    @property
    def sells(self) -> list[PlannedOrder]:
        return [o for o in self.orders if o.side == "sell"]

    def expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(NEW_YORK)
        return now >= datetime.fromisoformat(self.expires)


def next_session(broker: Broker, now: datetime) -> str:
    """The open a plan made now would trade at: today's, if it is a session and
    the market-on-open cutoff has not passed; otherwise the next one."""
    now = now.astimezone(NEW_YORK)
    days = broker.sessions(now.date().isoformat(), (now.date() + timedelta(days=10)).isoformat())
    today = now.date().isoformat()
    for d in days:
        if d > today or (d == today and now.time() < OPG_CUTOFF):
            return d
    raise RuntimeError("no trading session in the next ten days")


def session_after(broker: Broker, session: str, n: int) -> str:
    start = datetime.fromisoformat(session).date()
    days = [d for d in broker.sessions(session, (start + timedelta(days=n * 2 + 10)).isoformat()) if d > session]
    return days[n - 1]


def entries(config: dict, decision_date: str, rules: Rules) -> tuple[list[tuple[str, float]], list[str]]:
    """``([(symbol, size scale)], notes)`` from the standard screen dated ``decision_date``."""
    from tradingagents.agents.utils.memory import TradingMemoryLog
    from tradingagents.screener import run as sr
    from tradingagents.screener.manifest import load_manifests

    screens = [m for m in load_manifests(config, "") if m.as_of == decision_date and m.picks]
    if not screens:
        return [], [f"no standard screen dated {decision_date}"]
    m = screens[-1]
    if rules.entry == "screen":
        return [(s, 1.0) for s in m.pick_symbols], [f"screen {m.run_id}: every pick (screen mode)"]
    log = TradingMemoryLog({"memory_log_path": str(sr.sweep_log_path(m, config))})
    rated = {e["ticker"]: e.get("rating") for e in log.load_entries()
             if e["date"] == m.as_of and not (e.get("mandate") or "") and not e.get("superseded")}
    out, notes = [], [f"screen {m.run_id}"]
    undecided = [s for s in m.pick_symbols if s not in rated]
    if undecided:
        notes.append(f"not yet decided by the agents, so not traded: {', '.join(undecided)}")
    for s in m.pick_symbols:
        if rated.get(s) == "Buy":
            out.append((s, 1.0))
        elif rated.get(s) == "Overweight":
            out.append((s, rules.overweight_scale))
    return out, notes


def _last_price(symbol: str) -> float:
    from tradingagents.mandates.tools.financials import alpha_vantage_daily_strict

    frame = alpha_vantage_daily_strict(symbol)
    return float(frame["Raw Close"].iloc[-1]) if not frame.empty else math.nan


def size(picks: list[tuple[str, float]], account: Account, positions: list[Position], rules: Rules,
         price: Callable[[str], float], proceeds: float = 0.0) -> tuple[list[PlannedOrder], list[str]]:
    notes = []
    if not picks:
        return [], notes
    equity = account.equity
    per_name = min(equity / rules.cohorts / len(picks), equity * rules.max_position)
    invested = sum(p.market_value for p in positions if p.qty > 0) - proceeds
    room = min(account.cash + proceeds, equity - invested)
    held = {p.symbol for p in positions if p.qty}
    out = []
    for symbol, scale in picks:
        if symbol in held:
            notes.append(f"{symbol}: already held by a live cohort; not doubled")
            continue
        px = price(symbol)
        if not px or math.isnan(px) or px <= 0:
            notes.append(f"{symbol}: no price to size from; skipped")
            continue
        dollars = min(per_name * scale, room)
        qty = int(dollars // px)
        if qty < 1:
            notes.append(f"{symbol}: ${dollars:,.0f} buys no whole share at ${px:,.2f}; skipped")
            continue
        room -= qty * px
        out.append(PlannedOrder(symbol, "buy", qty, f"{'Buy' if scale == 1 else 'Overweight'} "
                                f"~${qty * px:,.0f} ({qty * px / equity:.1%} of equity)"))
    return out, notes


def build(config: dict, broker: Broker, book: Book, decision_date: str | None = None,
          now: datetime | None = None, price: Callable[[str], float] = _last_price,
          rules: Rules | None = None) -> OrderPlan:
    rules = rules or Rules.from_config(config)
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    session = next_session(broker, now)
    decision_date = decision_date or session
    account = broker.account()
    positions = broker.positions()
    pos = {p.symbol: p for p in positions}
    notes = []

    # Exits: every live cohort whose holding ends at this open.
    sells, exiting, proceeds = [], [], 0.0
    for c in book.live:
        if c.exit_session > session or c.exit_plan:
            continue
        exiting.append(c.plan_id)
        for symbol, qty in c.shares().items():
            have = pos.get(symbol)
            n = int(min(qty, have.qty if have else 0))
            if n < 1:
                notes.append(f"{symbol}: cohort {c.plan_id} expects {qty:g} shares, broker holds "
                             f"{have.qty if have else 0:g}; nothing to sell")
                continue
            sells.append(PlannedOrder(symbol, "sell", n, f"cohort {c.entry_session} ends"))
            proceeds += have.market_value * n / have.qty

    picks, more = entries(config, decision_date, rules)
    notes += more
    carried = {}
    for symbol, _ in picks:
        sell = next((o for o in sells if o.symbol == symbol), None)
        if sell:
            sells.remove(sell)
            carried[symbol] = sell.qty
            proceeds -= pos[symbol].market_value * sell.qty / pos[symbol].qty
            notes.append(f"{symbol}: picked again; {sell.qty} shares carried into the new cohort, not sold")
    picks = [(s, k) for s, k in picks if s not in carried]
    held_after = [p for p in positions if not any(s.symbol == p.symbol for s in sells)]
    buys, more = size(picks, account, held_after, rules, price, proceeds)
    notes += more

    plan_id = f"{session.replace('-', '')}-{STRATEGY}-{now:%H%M%S}"
    expires = datetime.combine(datetime.fromisoformat(session).date(), OPG_CUTOFF, NEW_YORK)
    plan = OrderPlan(
        id=plan_id, strategy=STRATEGY, decision_date=decision_date, session=session,
        expires=expires.isoformat(), created=now.isoformat(timespec="seconds"), equity=account.equity,
        orders=sells + buys, exit_cohorts=exiting, notes=notes, carried=carried,
        exit_session=session_after(broker, session, rules.hold_sessions) if (buys or carried) else "")
    if refusal := book.refusal():
        plan.notes.insert(0, f"Cannot be submitted as things stand: {refusal}")
    return plan


def save_plan(config: dict, plan: OrderPlan) -> None:
    (trading_dir(config) / "plans" / f"{plan.id}.json").write_text(json.dumps(asdict(plan), indent=2))


def load_plan(config: dict, plan_id: str) -> OrderPlan:
    path = trading_dir(config) / "plans" / f"{plan_id}.json"
    if not path.exists():
        raise ValueError(f"no order plan {plan_id!r}")
    data = json.loads(path.read_text())
    data["orders"] = [PlannedOrder(**o) for o in data["orders"]]
    return OrderPlan(**data)


def plans(config: dict) -> list[OrderPlan]:
    return [load_plan(config, p.stem) for p in sorted((trading_dir(config) / "plans").glob("*.json"))]


def latest_pending(config: dict, now: datetime | None = None) -> OrderPlan | None:
    live = [p for p in plans(config) if p.status == "pending" and not p.expired(now)]
    return live[-1] if live else None


def render(plan: OrderPlan) -> str:
    lines = [f"Order plan {plan.id} ({plan.status}) -- {plan.strategy}, decisions of {plan.decision_date}, "
             f"for the {plan.session} open; expires {plan.expires[11:16]} New York time.",
             f"Equity ${plan.equity:,.0f}. {len(plan.buys)} buys, {len(plan.sells)} sells, all market-on-open."]
    for o in plan.orders:
        lines.append(f"  {o.side.upper():4} {o.qty:>6} {o.symbol:<6} {o.reason}")
    if not plan.orders:
        lines.append("  (no orders)")
    lines += [f"  note: {n}" for n in plan.notes]
    if plan.status == "pending" and plan.orders:
        lines.append(f"Approve with: tradingagents trade submit {plan.id}  (or ask Hermes to submit it)")
    return "\n".join(lines)
