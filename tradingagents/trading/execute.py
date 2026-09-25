"""Submit an approved plan, halt, and reconcile the book against the broker.

Submitting is the approval: the CLI command is the operator's own hand, and
the MCP tool is behind Hermes' per-call confirmation. Each order carries a
client order id derived from the plan, so a repeated submit cannot place an
order twice -- the broker refuses a duplicate id.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .book import Cohort, load_book, save_book
from .broker import Broker, BrokerError
from .plan import NEW_YORK, OrderPlan, load_plan, save_plan

TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced"}


def _cid(plan: OrderPlan, i: int) -> str:
    return f"{plan.id}-{i}"


def submit(config: dict, broker: Broker, plan_id: str, now: datetime | None = None) -> str:
    plan = load_plan(config, plan_id)
    book = load_book(config)
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    if plan.status != "pending":
        return f"Plan {plan.id} is {plan.status}; nothing sent."
    if plan.expired(now):
        plan.status = "expired"
        save_plan(config, plan)
        return f"Plan {plan.id} expired at {plan.expires[11:16]} New York time; nothing sent."
    if refusal := book.refusal():
        return f"Nothing sent: {refusal}."
    if not broker.paper and not config.get("trading_live"):
        return "Nothing sent: the broker is not the paper endpoint and live trading is not enabled."
    account = broker.account()
    if account.blocked:
        return "Nothing sent: the broker reports the account blocked from trading."

    sent, errors = [], []
    # Sells first: at the open they settle together, but a refused sell should
    # be known before cash is committed.
    for i, o in sorted(enumerate(plan.orders), key=lambda x: x[1].side != "sell"):
        try:
            broker.submit(o.symbol, o.qty, o.side, _cid(plan, i))
            sent.append(o)
        except BrokerError as exc:
            errors.append(f"{o.side} {o.qty} {o.symbol}: {exc}")

    plan.status = "submitted"
    plan.submitted = now.isoformat(timespec="seconds")
    plan.submit_errors = errors
    save_plan(config, plan)

    for c in book.live:
        if c.plan_id in plan.exit_cohorts:
            c.exit_plan = plan.id
            for symbol in plan.carried:
                c.held.pop(symbol, None)
                c.planned.pop(symbol, None)
            if not c.shares():
                c.closed = True  # everything it held was carried into the new cohort
    bought = {o.symbol: o.qty for o in sent if o.side == "buy"}
    if bought or plan.carried:
        book.cohorts.append(Cohort(
            plan_id=plan.id, strategy=plan.strategy, entry_session=plan.session,
            exit_session=plan.exit_session, planned={**bought, **plan.carried},
            held={s: float(q) for s, q in plan.carried.items()}))
    if errors:
        book.blocked.append(f"plan {plan.id}: {len(errors)} order(s) refused at submit")
    save_book(config, book, "submitted", plan=plan.id, sent=len(sent), errors=errors)
    msg = f"Plan {plan.id}: sent {len(sent)} of {len(plan.orders)} orders for the {plan.session} open."
    return msg + (" Refused: " + "; ".join(errors) if errors else "")


def halt(config: dict, broker: Broker | None, reason: str) -> str:
    book = load_book(config)
    book.halted, book.halted_reason = True, reason or "operator"
    cancelled = 0
    note = ""
    if broker is not None:
        try:
            cancelled = broker.cancel_open()
        except BrokerError as exc:
            note = f" Could not cancel open orders: {exc}."
    save_book(config, book, "halted", reason=book.halted_reason, cancelled=cancelled)
    return (f"Trading halted ({book.halted_reason}). {cancelled} open order(s) cancelled; positions are "
            f"untouched.{note}")


def resume(config: dict) -> str:
    book = load_book(config)
    book.halted, book.halted_reason = False, ""
    save_book(config, book, "resumed")
    return "Trading resumed: plans can be submitted again."


def acknowledge(config: dict) -> str:
    book = load_book(config)
    cleared = list(book.blocked)
    book.blocked = []
    save_book(config, book, "acknowledged", cleared=cleared)
    return f"Acknowledged {len(cleared)} mismatch(es); plans can be submitted again."


def reconcile(config: dict, broker: Broker, now: datetime | None = None) -> list[str]:
    """Fills into the book, closed cohorts out of it, and every disagreement
    between the book and the broker reported -- and blocking -- until acknowledged."""
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    book = load_book(config)
    after = (now - timedelta(days=14)).astimezone(UTC).isoformat(timespec="seconds")
    by_cid = {o.client_order_id: o for o in broker.orders(after)}
    problems: list[str] = []
    from .plan import plans as all_plans

    for plan in all_plans(config):
        if plan.status != "submitted" or plan.session > now.date().isoformat():
            continue
        for i, o in enumerate(plan.orders):
            cid = _cid(plan, i)
            if cid in book.applied:
                continue  # booked on an earlier evening; each order is reported once
            got = by_cid.get(cid)
            if got is None:
                if not any(o.symbol in e for e in plan.submit_errors):
                    problems.append(f"{plan.id}: {o.side} {o.symbol} is not at the broker")
                book.applied.append(cid)
                continue
            if got.status not in TERMINAL:
                continue  # still working
            if got.filled_qty < o.qty:
                problems.append(f"{plan.id}: {o.side} {o.symbol} filled {got.filled_qty:g} of {o.qty} ({got.status})")
            book.applied.append(cid)
            for c in book.live:
                if o.side == "buy" and c.plan_id == plan.id:
                    c.held[o.symbol] = c.held.get(o.symbol, 0.0) + got.filled_qty
                elif o.side == "sell" and c.exit_plan == plan.id and o.symbol in c.shares():
                    held = c.shares()
                    left = held[o.symbol] - got.filled_qty
                    if left > 1e-9:
                        held[o.symbol] = left
                    else:
                        held.pop(o.symbol)
                    c.held, c.reconciled = held, True
    # A cohort is reconciled once every buy of its plan has reached a final state.
    for c in book.live:
        if not c.reconciled and all(
                _cid(p, i) in book.applied or o.side != "buy"
                for p in all_plans(config) if p.id == c.plan_id for i, o in enumerate(p.orders)):
            c.reconciled = True
        if c.exit_plan and c.reconciled and not c.held:
            c.closed = True

    expected: dict[str, float] = {}
    for c in book.live:
        for s, q in c.shares().items():
            expected[s] = expected.get(s, 0) + q
    actual = {p.symbol: p.qty for p in broker.positions()}
    for s in sorted(set(expected) | set(actual)):
        if abs(expected.get(s, 0) - actual.get(s, 0)) > 1e-6:
            problems.append(f"{s}: book holds {expected.get(s, 0):g}, broker {actual.get(s, 0):g}")

    new = [p for p in problems if p not in book.blocked]
    book.blocked += new
    book.last_reconciled = now.isoformat(timespec="seconds")
    save_book(config, book, "reconciled", problems=problems)
    return problems


def status(config: dict, broker: Broker | None = None) -> str:
    book = load_book(config)
    lines = [f"Book: {len(book.live)} live cohort(s); "
             f"{'HALTED (' + book.halted_reason + ')' if book.halted else 'trading allowed'}"
             f"{'; BLOCKED until acknowledged' if book.blocked else ''}; "
             f"last reconciled {book.last_reconciled or 'never'}."]
    for c in book.live:
        held = c.shares()
        lines.append(f"  cohort {c.entry_session} -> exits {c.exit_session}: "
                     + ", ".join(f"{s} {q:g}" for s, q in sorted(held.items()))
                     + (f" (selling in {c.exit_plan})" if c.exit_plan else ""))
    lines += [f"  mismatch: {b}" for b in book.blocked]
    if broker is not None:
        try:
            a = broker.account()
            lines.append(f"Account ({'paper' if broker.paper else 'LIVE'}): equity ${a.equity:,.0f}, "
                         f"cash ${a.cash:,.0f}, long ${a.long_market_value:,.0f}.")
        except BrokerError as exc:
            lines.append(f"Account: unavailable ({exc}).")
    return "\n".join(lines)
