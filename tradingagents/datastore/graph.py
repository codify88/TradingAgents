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


def _ref(row: dict) -> str:
    return hashlib.sha1(json.dumps(row, sort_keys=True).encode()).hexdigest()[:12]


def _ticker(symbol: str) -> str:
    return f"ticker:{symbol.upper()}"


def fill_insider(store, symbol: str, body: str) -> int:
    rows = json.loads(body).get("data") or []
    edges, nodes = [], {}
    for r in rows:
        name, day = normalise_name(r.get("executive", "")), r.get("transaction_date")
        if not name or not day:
            continue
        person = f"insider:{name}"
        nodes[person] = (person, "insider", {"name": r.get("executive")})
        edges.append((person, _ticker(symbol), "traded", day, _ref(r), insider_public(day),
                      "INSIDER_TRANSACTIONS",
                      {"title": r.get("executive_title"), "security": r.get("security_type"),
                       "side": r.get("acquisition_or_disposal"), "shares": r.get("shares"),
                       "price": r.get("share_price")}))
    store.upsert_nodes(list(nodes.values()))
    store.upsert_edges(edges)
    return len(edges)


def fill_congress(store, symbol: str, body: str) -> int:
    rows = json.loads(body).get("trades") or []
    edges, nodes = [], {}
    for r in rows:
        bio, day, filed = r.get("bioguide_id"), r.get("transaction_date"), r.get("filed_date")
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
    periods = {r.get("last_reported") for r in rows if r.get("last_reported")}
    conn = store._conn()
    for period in periods:
        conn.execute("DELETE FROM edge WHERE dst=? AND kind='holds' AND source='INSTITUTIONAL_HOLDINGS' "
                     "AND as_of=?", (_ticker(symbol), period))
    edges, nodes = [], {}
    for r in rows:
        name, period = normalise_name(r.get("holder_name", "")), r.get("last_reported")
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


def fill_politicians(store, body: str) -> int:
    rows = json.loads(body).get("politicians") or []
    store.upsert_nodes([(f"politician:{r['bioguide_id']}", "politician",
                         {"name": r.get("display_name"), "party": r.get("party"),
                          "chamber": r.get("chamber"), "state": r.get("state"),
                          "aliases": r.get("aliases") or []})
                        for r in rows if r.get("bioguide_id")])
    return len(rows)
