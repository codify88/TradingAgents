"""Fill the entity graph from harvested payloads.

Two tables (``node``, ``edge``), not a graph database: every edge carries
``as_of`` (when the fact was true) and ``available_at`` (when it became public),
so a historical query walks only the graph as it was known on its date.

Node ids are prefixed by kind: ``ticker:IBM``, ``insider:KRISHNA ARVIND``,
``politician:S000250``, ``institution:VANGUARD GROUP``, ``etf:SPY``,
``sector:TECHNOLOGY``. Insider and institution names are free text; the
normaliser below is deliberately conservative -- a mistake splits one entity in
two, never joins two different ones.

Design: docs/design/llmquant.md, "The entity graph".
"""

from __future__ import annotations

import hashlib
import json
import re

from tradingagents.harvest.datasets import insider_public, thirteen_f_public

_PUNCT = re.compile(r"[^A-Z0-9 ]+")
_SPACES = re.compile(r"\s+")
# Legal-form suffixes that vary between filings of one institution.
_SUFFIXES = (" INC", " LLC", " LP", " LTD", " CORP", " CO", " PLC", " NA", " N A", " THE")


def normalise_name(name: str) -> str:
    n = _SPACES.sub(" ", _PUNCT.sub(" ", (name or "").upper())).strip()
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if n.endswith(suffix):
                n, changed = n[: -len(suffix)].strip(), True
    return n


_DATE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})")


def clean_date(value) -> str | None:
    """The leading YYYY-MM-DD of a vendor date field, or None. Old insider rows
    carry raw Form 4 XML after the date ("2003-08-26</value><transactionCoding>...",
    seen for HD, JNJ and NTAP in the first harvest)."""
    m = _DATE.match(str(value or ""))
    return m.group(1) if m else None


def _ref(row: dict) -> str:
    return hashlib.sha1(json.dumps(row, sort_keys=True).encode()).hexdigest()[:12]


def _ticker(symbol: str) -> str:
    return f"ticker:{symbol.upper()}"


# The raw responses keep every insider row, and the insider tool reads them
# directly (two-business-day rule), so history needs no graph. The graph keeps
# what cross-name questions and the ownership watcher use: common-stock trades,
# one edge per person, day and side, for the last few years. All rows for 150
# names were 528,000 edges -- 42% of them grants, options and phantom units.
INSIDER_GRAPH_YEARS = 3
_COMMON = re.compile(r"common|ordinary|class [a-c]\b|capital stock", re.I)


def fill_insider(store, symbol: str, body: str, today: str | None = None) -> int:
    import datetime as _dt

    rows = json.loads(body).get("data") or []
    since = ((_dt.date.fromisoformat(today) if today else _dt.date.today())
             - _dt.timedelta(days=365 * INSIDER_GRAPH_YEARS)).isoformat()
    trades: dict[tuple, dict] = {}
    nodes = {}
    for r in rows:
        name, day = normalise_name(r.get("executive", "")), clean_date(r.get("transaction_date"))
        side = r.get("acquisition_or_disposal")
        if not name or not day or day < since or side not in ("A", "D") \
                or not _COMMON.search(r.get("security_type") or ""):
            continue
        person = f"insider:{name}"
        nodes[person] = (person, "insider", {"name": r.get("executive")})
        t = trades.setdefault((person, day, side), {"title": r.get("executive_title"), "shares": 0.0,
                                                     "rows": 0, "price": r.get("share_price")})
        try:
            t["shares"] += float(r.get("shares") or 0)
        except (TypeError, ValueError):
            pass
        t["rows"] += 1
    conn = store._conn()
    conn.execute("DELETE FROM edge WHERE dst=? AND source='INSIDER_TRANSACTIONS'", (_ticker(symbol),))
    edges = [(person, _ticker(symbol), "traded", day, side, insider_public(day), "INSIDER_TRANSACTIONS",
              {"title": t["title"], "side": side, "shares": round(t["shares"], 2), "price": t["price"],
               "rows": t["rows"]})
             for (person, day, side), t in trades.items()]
    store.upsert_nodes(list(nodes.values()))
    store.upsert_edges(edges)
    return len(edges)


def fill_congress(store, symbol: str, body: str) -> int:
    rows = json.loads(body).get("trades") or []
    edges, nodes = [], {}
    for r in rows:
        bio, day, filed = r.get("bioguide_id"), clean_date(r.get("transaction_date")), clean_date(r.get("filed_date"))
        if not bio or not day or not filed:
            continue
        pol = f"politician:{bio}"
        # POLITICIAN_METADATA holds the fuller record (aliases, terms); never overwrite it.
        if pol not in nodes and store.node(pol) is None:
            nodes[pol] = (pol, "politician", {"name": r.get("politician_canonical"),
                                              "party": r.get("party"), "chamber": r.get("chamber"),
                                              "state": r.get("state")})
        edges.append((pol, _ticker(symbol), "traded", day, _ref(r), filed, "CONGRESS_TRADES",
                      {"type": r.get("transaction_type"), "amount_min": r.get("amount_min"),
                       "amount_max": r.get("amount_max"), "owner": r.get("owner_code"),
                       "filing_status": r.get("filing_status")}))
    store.upsert_nodes(list(nodes.values()))
    store.upsert_edges(edges)
    return len(edges)


# A snapshot lists every 13F filer holding the name -- about 2,000 for a large
# company. The complete list stays in the raw snapshot; the graph keeps the
# largest holders, which is what "who owns this" and "who added" need. All of
# them would be ~5M edges (~1.9 GB) per quarter across the universe.
INSTITUTIONS_PER_TICKER = 100


def _shares(r: dict) -> float:
    try:
        return float(r.get("shares_held") or 0)
    except (TypeError, ValueError):
        return 0.0


def fill_institutional(store, symbol: str, body: str) -> int:
    rows = json.loads(body).get("holdings") or []
    rows = sorted(rows, key=_shares, reverse=True)[:INSTITUTIONS_PER_TICKER]
    # The top holders change between snapshots of one quarter; replace, don't accumulate.
    periods = {clean_date(r.get("last_reported")) for r in rows} - {None}
    conn = store._conn()
    for period in periods:
        conn.execute("DELETE FROM edge WHERE dst=? AND kind='holds' AND source='INSTITUTIONAL_HOLDINGS' "
                     "AND as_of=?", (_ticker(symbol), period))
    edges, nodes = [], {}
    for r in rows:
        name, period = normalise_name(r.get("holder_name", "")), clean_date(r.get("last_reported"))
        if not name or not period:
            continue
        inst = f"institution:{name}"
        nodes[inst] = (inst, "institution", {"name": r.get("holder_name")})
        edges.append((inst, _ticker(symbol), "holds", period, "", thirteen_f_public(period),
                      "INSTITUTIONAL_HOLDINGS",
                      {"shares": r.get("shares_held"), "change": r.get("shares_changed"),
                       "change_type": r.get("change_type")}))
    store.upsert_nodes(list(nodes.values()))
    store.upsert_edges(edges)
    return len(edges)


def fill_etf(store, etf: str, body: str, snapshot_day: str) -> int:
    payload = json.loads(body)
    etf_id = f"etf:{etf.upper()}"
    store.upsert_node(etf_id, "etf", {"net_assets": payload.get("net_assets"),
                                      "leveraged": payload.get("leveraged")})
    edges = [(etf_id, _ticker(h["symbol"]), "holds", snapshot_day, "", snapshot_day, "ETF_PROFILE",
              {"weight": h.get("weight")})
             for h in payload.get("holdings") or [] if h.get("symbol") and h["symbol"] != "n/a"]
    store.upsert_edges(edges)
    return len(edges)


def fill_overview(store, symbol: str, body: str, fetched_day: str) -> int:
    payload = json.loads(body)
    sector, industry = payload.get("Sector"), payload.get("Industry")
    store.upsert_node(_ticker(symbol), "ticker", {"name": payload.get("Name"),
                                                  "exchange": payload.get("Exchange")})
    edges = []
    for kind, value in (("in_sector", sector), ("in_industry", industry)):
        if value and value != "None":
            node = f"{kind[3:]}:{value.upper()}"
            store.upsert_node(node, kind[3:])
            # Classification is today's, not the past's: dated when fetched.
            edges.append((_ticker(symbol), node, kind, fetched_day, "", fetched_day, "OVERVIEW", {}))
    store.upsert_edges(edges)
    return len(edges)


def rebuild(store, vendor: str = "alpha_vantage") -> dict[str, int]:
    """Refill the graph from every stored insider, congressional, overview and
    politician response and each symbol's latest holdings and ETF snapshot --
    after a parser fix, without a single request."""
    import datetime as _dt

    from tradingagents.datastore.store import params_key

    conn = store._conn()
    counts: dict[str, int] = {}

    def bodies(endpoint):
        for key, symbol in conn.execute(
                "SELECT params_key, symbol FROM response WHERE vendor=? AND endpoint=?", (vendor, endpoint)):
            row = store.get(vendor, endpoint, key)
            if row is not None:
                yield symbol, row[0], row[1]

    fills = {"INSIDER_TRANSACTIONS": fill_insider, "CONGRESS_TRADES": fill_congress}
    for _, body, _ in bodies("POLITICIAN_METADATA"):
        counts["POLITICIAN_METADATA"] = counts.get("POLITICIAN_METADATA", 0) + fill_politicians(store, body)
    for endpoint, fill in fills.items():
        for symbol, body, _ in bodies(endpoint):
            try:
                counts[endpoint] = counts.get(endpoint, 0) + fill(store, symbol, body)
            except (ValueError, TypeError, AttributeError):
                counts[endpoint + " (unreadable)"] = counts.get(endpoint + " (unreadable)", 0) + 1
    for symbol, body, fetched in bodies("OVERVIEW"):
        try:
            fill_overview(store, symbol, body, fetched.astimezone(_dt.UTC).date().isoformat())
            counts["OVERVIEW"] = counts.get("OVERVIEW", 0) + 1
        except (ValueError, TypeError, AttributeError):
            pass
    for endpoint in ("INSTITUTIONAL_HOLDINGS", "ETF_PROFILE"):
        symbols = [r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM snapshot WHERE vendor=? AND endpoint=?", (vendor, endpoint))]
        for symbol in symbols:
            snaps = store.snapshots(vendor, endpoint, params_key({"symbol": symbol}))
            if not snaps:
                continue
            day, body = snaps[-1]
            try:
                n = (fill_institutional(store, symbol, body) if endpoint == "INSTITUTIONAL_HOLDINGS"
                     else fill_etf(store, symbol, body, day))
                counts[endpoint] = counts.get(endpoint, 0) + n
            except (ValueError, TypeError, AttributeError):
                pass
    return counts


def fill_politicians(store, body: str) -> int:
    rows = json.loads(body).get("politicians") or []
    store.upsert_nodes([(f"politician:{r['bioguide_id']}", "politician",
                         {"name": r.get("display_name"), "party": r.get("party"),
                          "chamber": r.get("chamber"), "state": r.get("state"),
                          "aliases": r.get("aliases") or []})
                        for r in rows if r.get("bioguide_id")])
    return len(rows)
