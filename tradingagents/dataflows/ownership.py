"""Point-in-time ownership and earnings-call data, from the harvest's store.

Each function answers "as of ``curr_date``" and shows only what could have been
read on that date, by the rule in ``tradingagents.harvest.datasets``:

- congressional trades by their filing date, not the trade date;
- institutional holdings only from a snapshot taken on or before the date, and
  only holders whose 13F was due by then (quarter end + 45 days) -- the vendor
  keeps no history, so a date before the first snapshot has no answer;
- ETF exposure from the latest ETF snapshot on or before the date;
- the latest earnings call held before the date.

Registered as the optional ``ownership_data`` category: a failure here returns a
notice rather than aborting a decision. Not yet given to any analyst -- which
role reads which of these is a mandate decision.
"""

from __future__ import annotations

import json

from .alpha_vantage_common import _make_api_request

MAX_ROWS = 25
MAX_CALL_CHARS = 12_000


def _today() -> str:
    from tradingagents.dataflows.utils import get_current_date

    return get_current_date()


def get_congress_trades(symbol: str, curr_date: str) -> str:
    """Stock trades disclosed by members of Congress, filed on or before ``curr_date``."""
    symbol = symbol.strip().upper()
    payload = json.loads(_make_api_request("CONGRESS_TRADES", {"symbol": symbol}))
    trades = [t for t in payload.get("trades") or [] if (t.get("filed_date") or "9999") <= curr_date]
    if not trades:
        return f"No congressional trades in {symbol} disclosed by {curr_date}."
    trades.sort(key=lambda t: t["filed_date"], reverse=True)
    lines = [f"# Congressional trades in {symbol}, disclosed by {curr_date} "
             f"({len(trades)} filings; newest {min(len(trades), MAX_ROWS)} shown)",
             "Dated by filing: a trade is public on its filed date, often weeks after the trade.",
             "", "| Filed | Traded | Member | Party | Chamber | Type | Amount | Owner |",
             "|---|---|---|---|---|---|---|---|"]
    for t in trades[:MAX_ROWS]:
        amount = f"${float(t.get('amount_min') or 0):,.0f}-${float(t.get('amount_max') or 0):,.0f}"
        lines.append(f"| {t['filed_date']} | {t.get('transaction_date')} | {t.get('politician_canonical')} | "
                     f"{t.get('party')} | {t.get('chamber')} | {t.get('transaction_type')} | {amount} | "
                     f"{t.get('owner_code')} |")
    return "\n".join(lines)


def get_institutional_holdings(symbol: str, curr_date: str) -> str:
    """The largest institutional holders public by ``curr_date``, from the latest
    snapshot taken on or before it."""
    from tradingagents.datastore import get_store, store_mode
    from tradingagents.datastore.store import params_key
    from tradingagents.harvest.datasets import VENDOR, thirteen_f_public

    symbol = symbol.strip().upper()
    snaps = []
    if store_mode() != "off":
        snaps = [s for s in get_store().snapshots(VENDOR, "INSTITUTIONAL_HOLDINGS",
                                                  params_key({"symbol": symbol})) if s[0] <= curr_date]
    if snaps:
        taken, body = snaps[-1]
    elif curr_date >= _today():
        taken, body = curr_date, _make_api_request("INSTITUTIONAL_HOLDINGS", {"symbol": symbol})
    else:
        return (f"No institutional holdings snapshot of {symbol} exists from on or before {curr_date}: "
                "the vendor keeps no history, so holdings are known only from the day the harvest "
                "first snapshotted them.")
    rows = [r for r in json.loads(body).get("holdings") or []
            if r.get("last_reported") and thirteen_f_public(r["last_reported"]) <= curr_date]
    if not rows:
        return f"No institutional holdings of {symbol} were public by {curr_date}."

    def shares(r):
        try:
            return float(r.get("shares_held") or 0)
        except (TypeError, ValueError):
            return 0.0

    rows.sort(key=shares, reverse=True)
    changes = {}
    for r in rows:
        changes[r.get("change_type") or "unknown"] = changes.get(r.get("change_type") or "unknown", 0) + 1
    lines = [f"# Institutional holders of {symbol} as of {curr_date} (snapshot {taken})",
             f"{len(rows):,} holders public by then; changes: "
             + ", ".join(f"{k} {v:,}" for k, v in sorted(changes.items())),
             "", "| Holder | Shares | Change | Change type | Period |", "|---|---|---|---|---|"]
    for r in rows[:MAX_ROWS]:
        lines.append(f"| {r.get('holder_name')} | {shares(r):,.0f} | {r.get('shares_changed')} | "
                     f"{r.get('change_type')} | {r.get('last_reported')} |")
    return "\n".join(lines)


def get_etf_exposure(symbol: str, curr_date: str) -> str:
    """ETFs holding ``symbol``, by weight, from the latest ETF snapshots on or before ``curr_date``."""
    from tradingagents.datastore import get_store, store_mode

    symbol = symbol.strip().upper()
    if store_mode() == "off":
        return "ETF exposure needs the data store, which is off."
    edges = [e for e in get_store().edges_to(f"ticker:{symbol}", "holds", curr_date)
             if e["src"].startswith("etf:")]
    latest: dict[str, dict] = {}
    for e in edges:  # ordered by as_of; keep each ETF's latest snapshot
        latest[e["src"]] = e
    if not latest:
        return f"No ETF snapshot on or before {curr_date} holds {symbol}."

    def weight(e):
        try:
            return float(e.get("weight") or 0)
        except (TypeError, ValueError):
            return 0.0

    ranked = sorted(latest.values(), key=weight, reverse=True)
    lines = [f"# ETFs holding {symbol} as of {curr_date} ({len(ranked)} ETFs)", "",
             "| ETF | Weight | Snapshot |", "|---|---|---|"]
    lines += [f"| {e['src'][4:]} | {weight(e):.2%} | {e['as_of']} |" for e in ranked[:MAX_ROWS]]
    return "\n".join(lines)


def get_earnings_call(symbol: str, curr_date: str) -> str:
    """The most recent earnings call held before ``curr_date``: speakers and what they said."""
    from tradingagents.datastore import get_store, store_mode
    from tradingagents.harvest.run import transcript_quarters

    symbol = symbol.strip().upper()
    _make_api_request("EARNINGS", {"symbol": symbol})
    _make_api_request("OVERVIEW", {"symbol": symbol})
    if store_mode() == "off":
        return "Earnings calls need the data store, which is off."
    held = [(label, day) for label, day in transcript_quarters(get_store(), symbol, curr_date)]
    if not held:
        return f"No earnings call for {symbol} before {curr_date}."
    label, day = held[0]
    body = json.loads(_make_api_request("EARNINGS_CALL_TRANSCRIPT", {"symbol": symbol, "quarter": label}))
    turns = body.get("transcript") or []
    if not turns:
        return f"No transcript is available for {symbol}'s {label} call ({day})."
    out = [f"# {symbol} earnings call, fiscal {label}, held {day}", ""]
    size = 0
    for t in turns:
        line = f"**{t.get('speaker')}** ({t.get('title')}): {t.get('content')}"
        size += len(line)
        if size > MAX_CALL_CHARS:
            out.append(f"[... cut at {MAX_CALL_CHARS:,} characters of {len(turns)} turns]")
            break
        out.append(line)
    return "\n\n".join(out)

