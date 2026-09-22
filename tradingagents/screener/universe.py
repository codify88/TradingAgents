"""Tier 0: the investable universe, before any market data is fetched.

Alpha Vantage's LISTING_STATUS is one keyless-per-run CSV covering every active
US listing. Filtering here is deliberately crude and cheap -- exchange, asset
type, listing age -- because everything downstream costs either a price
download or an API call per name.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

import pandas as pd

from tradingagents.dataflows.alpha_vantage_common import _make_api_request
from tradingagents.dataflows.utils import get_current_date

# Venues whose listings have the disclosure and liquidity regime this is built
# for. OTC is excluded at the universe level rather than by a later liquidity
# filter: the data quality problem is categorical, not a matter of degree.
DEFAULT_EXCHANGES = ("NYSE", "NASDAQ", "AMEX", "NYSE MKT", "BATS")

# A company needs enough reported history for the mandates' screens to mean
# anything -- quality metrics read a decade, momentum reads a year.
MIN_LISTING_AGE_DAYS = 400

# The earliest date Alpha Vantage's LISTING_STATUS will answer for.
EARLIEST_HISTORICAL_DATE = "2010-01-01"

# NASDAQ's fifth-letter codes for securities that are not common shares:
# W warrants, R rights, U units, Z other (notes), P/O/N/M preferred series.
# Class-share letters (A, B, C, K, L) are deliberately absent: GOOGL is
# Alphabet's Class A common, not a derivative of GOOG.
_DERIVATIVE_SUFFIXES = frozenset("WRUZPONM")

# Instrument words as filers actually spell them. Tight on purpose: "Preferred
# Bank" and "Unit Corporation" are common stocks, so a bare word never matches.
_DERIVATIVE_NAME = re.compile(
    r"\bwarrants?\b|\bwts?\s+(exp|pur)\b|\brights?\b|\brts?\s*$"
    r"|-\s*units?\b|\bunits?\s*[(\d]|\bunits?\s*$|\btangible\s+equity\s+units?\b"
    r"|\bdepositary\b|\bpfd\b|\bpreferred\s+(stock|shares|securities)\b"
    r"|\bseries\s+[a-z]\s+preferred\b|\d+(\.\d+)?%\s|\bnotes\s+due\b"
    r"|\bsr\s+nts?\b|\bdebentures\b",
    re.IGNORECASE,
)


# Pooled vehicles Alpha Vantage also files as "Stock": ETFs, exchange-traded
# notes, leveraged and inverse products, closed-end funds and term trusts
# ("TRADR 2X LONG CEG DAILY ETF", "MicroSectors Travel 3X Leveraged ETNs",
# "BlackRock Science and Technology Term Trust"). A mandate is written about
# an operating business. REITs and royalty trusts ("... Realty Trust") are
# common shares and must not match: only a *term* trust or a name *ending* in
# Fund does.
_POOLED_NAME = re.compile(
    r"\bETFs?\b|\bETNs?\b|\bleveraged\b|\binverse\b|\b\d+(\.\d+)?x\s+(long|short)\b"
    r"|\bterm\s+trust\b|\bfund\.?\s*$",
    re.IGNORECASE,
)


def pooled_vehicle(name: str) -> bool:
    """Whether the filed name is an ETF, note, leveraged product or fund, not a company."""
    return bool(_POOLED_NAME.search(name.strip()))


_SHARE_CLASS = re.compile(r"\s*[-,]?\s*\bclass\s+[a-z]\b.*$", re.IGNORECASE)


def company_key(name: str) -> str:
    """The company a listing belongs to, with any share-class suffix removed.

    "Alphabet Inc - Class A" and "Alphabet Inc - Class C" are one company: a
    screen that picks both has spent a slot on the same bet twice.
    """
    return _SHARE_CLASS.sub("", name).strip().lower()


def one_line_per_issuer(candidates: list[Candidate]) -> list[Candidate]:
    """Resolve symbols and names that appear more than once on the listing date.

    - One symbol listed twice under the same name (OKE, TTE) is one company:
      keep one row. Under *different* names it is a reused ticker (DFNS was
      both IronNet and T3 Defense): its price history cannot be attributed to
      either, so neither is kept.
    - One name filed under several symbols is several lines of one issuer:
      the notes and preferreds whose suffix the structural rule misses (ADAM,
      ADAMI, ADAML) and the exchange-traded notes filed under the issuing
      bank's name (GDXD as "Bank of Montreal"). Keep the shortest symbol --
      the common shares -- and drop the rest.
    """
    by_symbol: dict[str, list[Candidate]] = {}
    for c in candidates:
        by_symbol.setdefault(c.symbol, []).append(c)
    unique = []
    for rows in by_symbol.values():
        if len({r.name.strip().lower() for r in rows}) == 1:
            unique.append(min(rows, key=lambda r: r.ipo_date))
    by_name: dict[str, list[Candidate]] = {}
    for c in unique:
        by_name.setdefault(c.name.strip().lower(), []).append(c)
    kept = [min(rows, key=lambda r: (len(r.symbol), r.symbol)) for rows in by_name.values()]
    return sorted(kept, key=lambda c: c.symbol)


def derivative_line(symbol: str, name: str, listed: set[str]) -> str | None:
    """Why ``symbol`` is a warrant, right, unit, note or preferred -- or None.

    Alpha Vantage files these under assetType "Stock". Dashed symbols are caught
    upstream; these are the undashed ones, about one "stock" row in eight. Two
    signals, either sufficient:

    * structure: a five-letter symbol that is another listed symbol plus a
      reserved suffix (ZION+O, AGNC+N, AEP+PZ). This catches the many filed
      under the issuer's plain name, which no name rule could.
    * the filed name: "- Warrants (30/06/2028)", "Units (1 Ord Cls A ...)".

    Checked against the full listing: of the structural matches whose names
    carry no instrument word, every one read as a preferred, right, note or
    unit once the two-letter case required P plus a reserved letter (which
    keeps MARPS, a royalty trust's common units, from reading as MAR + PS).
    """
    if len(symbol) == 5:
        if symbol[4] in _DERIVATIVE_SUFFIXES and symbol[:4] in listed:
            return f"{symbol[:4]} line {symbol[4]}"
        if symbol[3] == "P" and symbol[4] in _DERIVATIVE_SUFFIXES and symbol[:3] in listed:
            return f"{symbol[:3]} line {symbol[3:]}"
    if _DERIVATIVE_NAME.search(name):
        return "named as a warrant, right, unit, note or preferred"
    return None


@dataclass(frozen=True)
class Candidate:
    """One symbol under consideration, with why it survived or did not."""

    symbol: str
    name: str
    exchange: str
    ipo_date: str
    # Listed on the screen date but no longer active today. Only a historical
    # screen can see these, and they are exactly the names a survivors-only
    # universe silently drops.
    delisted_since: bool = False

    def __str__(self) -> str:
        return self.symbol


def _rows(csv_text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(csv_text)))


def load_universe(
    as_of: str,
    exchanges: tuple[str, ...] = DEFAULT_EXCHANGES,
    min_age_days: int = MIN_LISTING_AGE_DAYS,
    limit: int | None = None,
) -> list[Candidate]:
    """Active common stocks listed long enough to have a history, as of a date.

    A past ``as_of`` asks Alpha Vantage for the listings active *on that date*,
    not today's: screening 2022 against today's listings would leave out every
    company that has since been acquired, taken private or gone bankrupt, and
    the screen's history would be a study of survivors. Those names are marked
    ``delisted_since`` so later tiers can report what became of them.

    ``limit`` truncates deterministically (alphabetically) rather than randomly,
    so a capped run is reproducible; it exists for cheap end-to-end runs, not
    as a sampling strategy.
    """
    cutoff = pd.Timestamp(as_of) - pd.Timedelta(days=min_age_days)
    allowed = {e.upper() for e in exchanges}

    active_now: set[str] | None = None
    if as_of < get_current_date():
        if as_of < EARLIEST_HISTORICAL_DATE:
            raise ValueError(
                f"historical listings start at {EARLIEST_HISTORICAL_DATE}; "
                f"cannot screen as of {as_of}"
            )
        listing = _make_api_request("LISTING_STATUS", {"date": as_of, "state": "active"})
        active_now = {
            r.get("symbol", "") for r in _rows(_make_api_request("LISTING_STATUS", {}))
            if r.get("status") == "Active"
        }
    else:
        listing = _make_api_request("LISTING_STATUS", {})

    rows = _rows(listing)
    listed_symbols = {r.get("symbol", "") for r in rows if r.get("assetType") == "Stock"}
    out = []
    for row in rows:
        if row.get("status") != "Active" or row.get("assetType") != "Stock":
            continue
        # Alpha Vantage files warrants, units, rights and preferred series under
        # assetType "Stock", marking the class with a dash (AAC-WS, ABR-P-D).
        # They are not the common shares any mandate is written about, and none
        # of them resolve to a price series, so they would otherwise cost a
        # download apiece only to be dropped as "no price history".
        if "-" in row.get("symbol", ""):
            continue
        if row.get("exchange", "").upper() not in allowed:
            continue
        # The undashed warrants, rights, units, notes and preferreds: not the
        # common shares any mandate is written about, and each would otherwise
        # cost a price download only to be dropped as "no price history".
        if derivative_line(row.get("symbol", ""), row.get("name", ""), listed_symbols):
            continue
        if pooled_vehicle(row.get("name", "")):
            continue
        ipo = row.get("ipoDate") or ""
        try:
            listed = pd.Timestamp(ipo)
        except (ValueError, TypeError):
            continue  # an unparseable listing date is not a usable history
        # pd.Timestamp("") is NaT, and every NaT comparison is False, so a
        # missing date would slip through the age check rather than fail it.
        if pd.isna(listed) or listed > cutoff:
            continue
        # A symbol delisted before the run date was never investable on it.
        delisted = row.get("delistingDate")
        if delisted and delisted != "null" and pd.Timestamp(delisted) <= pd.Timestamp(as_of):
            continue
        out.append(Candidate(
            symbol=row["symbol"], name=row.get("name", ""),
            exchange=row.get("exchange", ""), ipo_date=ipo,
            delisted_since=active_now is not None and row["symbol"] not in active_now,
        ))

    out = one_line_per_issuer(out)
    return out[:limit] if limit else out
