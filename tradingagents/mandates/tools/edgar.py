"""Statements for companies Alpha Vantage no longer covers, from SEC EDGAR.

Alpha Vantage returns empty statements once a company delists, so every
historical question about a company that later disappeared -- acquired, taken
private, bankrupt -- used to end at "fundamentals unavailable", and the
screener's fundamentals tier kept only survivors. EDGAR keeps a delisted
filer's XBRL facts indefinitely.

Two problems had to be solved:

1. **Which filer is it?** EDGAR maps *current* tickers to filers only; a dead
   ticker is in no SEC table. The company's name comes from Alpha Vantage's
   delisted listing (with IPO and delisting dates, so a reused ticker resolves
   to the right company), and is matched against every name that has ever filed
   (``cik-lookup-data.txt``). A name can match several filers -- "Aaron's" is
   three -- so candidates must also have been filing 10-Ks or 10-Qs around the
   analysis date. Anything short of exactly one match is "no data", never a
   guess: another company's statements are worse than none.

2. **What was known when?** Every EDGAR fact carries the date it was filed, so
   a period is admitted only once filed -- and a later restatement is used only
   from the date it was filed. Quarterly cash flows are filed year-to-date, so
   single quarters are derived from the year-to-date chain; summing them raw
   would count the first quarter four times in a trailing year.

The result is the same :class:`Financials` the Alpha Vantage loader builds,
with Alpha Vantage's field names, so every tool reads it unchanged. US-GAAP
filers only: an IFRS filer (a UK or Dutch company) has no us-gaap facts and
stays "no data".
"""

from __future__ import annotations

import csv
import functools
import io
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from .financials import Financials, FinancialsUnavailable

logger = logging.getLogger(__name__)

_LOOKUP_URL = "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_LOOKUP_TTL_SECONDS = 7 * 24 * 60 * 60   # entity names change slowly

_PERIODIC_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "10-KT", "10-KT/A"}

# Alpha Vantage field <- us-gaap tags, first present wins. Duration facts are
# flows over a period; instants are balances at a date.
_DURATION = {
    "totalRevenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                     "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"),
    "costOfRevenue": ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"),
    "grossProfit": ("GrossProfit",),
    "operatingIncome": ("OperatingIncomeLoss",),
    "netIncome": ("NetIncomeLoss", "ProfitLoss"),
    "incomeTaxExpense": ("IncomeTaxExpenseBenefit",),
    "incomeBeforeTax": (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ),
    "interestExpense": ("InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"),
    "depreciationAndAmortization": ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
                                    "DepreciationAmortizationAndAccretionNet", "Depreciation"),
    "operatingCashflow": ("NetCashProvidedByUsedInOperatingActivities",
                          "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    "capitalExpenditures": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"),
    "dividendPayout": ("PaymentsOfDividends", "PaymentsOfDividendsCommonStock"),
    "paymentsForRepurchaseOfCommonStock": ("PaymentsForRepurchaseOfCommonStock",),
    "cashflowFromInvestment": ("NetCashProvidedByUsedInInvestingActivities",
                               "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"),
    "proceedsFromIssuanceOfCommonStock": ("ProceedsFromIssuanceOfCommonStock",),
    "proceedsFromIssuanceOfLongTermDebtAndCapitalSecuritiesNet": ("ProceedsFromIssuanceOfLongTermDebt",),
}
_INSTANT = {
    "totalShareholderEquity": ("StockholdersEquity",
                               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    "longTermDebt": ("LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations", "LongTermDebt"),
    "cashAndShortTermInvestments": ("CashAndCashEquivalentsAtCarryingValue",
                                    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    "goodwill": ("Goodwill",),
    "intangibleAssetsExcludingGoodwill": ("IntangibleAssetsNetExcludingGoodwill",
                                          "FiniteLivedIntangibleAssetsNet"),
    "currentNetReceivables": ("AccountsReceivableNetCurrent", "ReceivablesNetCurrent"),
    "inventory": ("InventoryNet",),
    "totalAssets": ("Assets",),
}

# Short-term debt is two things filers tag separately: long-term debt falling due
# within the year, and borrowings that were short-term from the start. Each group
# picks its tag per period; the field is their sum.
_SHORT_TERM_DEBT_GROUPS = (
    ("LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent", "DebtCurrent"),
    ("CommercialPaper", "ShortTermBorrowings", "LoansAndNotesPayable", "OtherShortTermBorrowings"),
)
_SHARES_CURRENT = ("CommonStockSharesOutstanding",)

_ANNUAL_DAYS = (350, 380)
_QUARTER_DAYS = (80, 100)


# --- names -------------------------------------------------------------------------

_SUFFIX_WORDS = {"CORPORATION": "CORP", "INCORPORATED": "INC", "COMPANY": "CO", "LIMITED": "LTD"}
_TRAILING_FORMS = {"INC", "CORP", "CO", "LTD", "PLC", "LP", "LLC", "NV", "SA", "AG", "THE"}


def normalize_name(name: str) -> str:
    """One spelling for a company name, however a vendor wrote it.

    "Aarons Company Inc (The)" (Alpha Vantage) and "AARON'S COMPANY, INC." (SEC)
    both become "AARONS CO INC". Share-class tails (" - Class A") are dropped:
    the filer is the company, not the class.
    """
    s = name.upper()
    s = re.sub(r"\s+-\s+(CLASS|SERIES|CL)\b.*$", "", s)
    s = s.replace("(THE)", " ").replace("&", " AND ")
    s = re.sub(r"^THE\s+", "", s)
    s = re.sub(r"[^A-Z0-9 ]+", "", s)
    words = [_SUFFIX_WORDS.get(w, w) for w in s.split()]
    return " ".join(words)


def _stem(normalized: str) -> str:
    """The name without its legal-form tail: "ATLAS AIR WORLDWIDE HOLDINGS INC" -> "... HOLDINGS"."""
    words = normalized.split()
    while words and words[-1] in _TRAILING_FORMS:
        words.pop()
    return " ".join(words)


# --- SEC access ----------------------------------------------------------------------


def _cache_dir() -> Path:
    from tradingagents.dataflows.config import get_config

    return Path(get_config()["data_cache_dir"]) / "sec_edgar"


def _sec_json(url: str, name: str) -> dict:
    # Upstream's cached, identified fetch: SEC requires a User-Agent with a
    # contact (SEC_EDGAR_USER_AGENT) and throttles anonymous clients.
    from tradingagents.dataflows.sec_edgar import _cached_json

    return _cached_json(url, name)


@functools.lru_cache(maxsize=1)
def _name_index() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Every name that has ever filed -> its CIKs, by exact name and by stem."""
    from tradingagents.dataflows.sec_edgar import _user_agent

    path = _cache_dir() / "cik-lookup-data.txt"
    if not path.exists() or time.time() - path.stat().st_mtime > _LOOKUP_TTL_SECONDS:
        response = requests.get(_LOOKUP_URL, headers={"User-Agent": _user_agent()}, timeout=120)
        response.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
    exact: dict[str, set[str]] = {}
    stems: dict[str, set[str]] = {}
    for line in path.read_text(encoding="latin-1").splitlines():
        # "NAME:0001234567:" -- names may themselves contain colons.
        parts = line.rstrip(":").rsplit(":", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        key = normalize_name(parts[0])
        cik = parts[1].zfill(10)
        exact.setdefault(key, set()).add(cik)
        stems.setdefault(_stem(key), set()).add(cik)
    return exact, stems


@functools.lru_cache(maxsize=1)
def _delisted_listing() -> list[dict]:
    from tradingagents.dataflows.alpha_vantage_common import _make_api_request

    return list(csv.DictReader(io.StringIO(_make_api_request("LISTING_STATUS", {"state": "delisted"}))))


def delisted_name(symbol: str, as_of: str) -> str | None:
    """The company that traded as ``symbol`` on ``as_of``, if it has since delisted.

    Tickers are reused, so the row must have been listed on the date asked
    about; failing that (a question after the delisting), the latest listing
    that began before it.
    """
    symbol = symbol.strip().upper()
    rows = [r for r in _delisted_listing() if r.get("symbol") == symbol and r.get("assetType") == "Stock"]
    live = [r for r in rows if (r.get("ipoDate") or "") <= as_of <= (r.get("delistingDate") or "9999")]
    if not live:
        live = sorted((r for r in rows if (r.get("ipoDate") or "") <= as_of),
                      key=lambda r: r.get("ipoDate") or "")[-1:]
    return live[0]["name"] if len(live) == 1 else None


def _was_filing(cik: str, as_of: str) -> bool:
    """Whether this filer was filing periodic reports around ``as_of``."""
    try:
        recent = _sec_json(_SUBMISSIONS_URL.format(cik=cik), f"submissions_{cik}.json")["filings"]["recent"]
    except Exception:
        return False
    cutoff = pd.Timestamp(as_of)
    window = cutoff - pd.Timedelta(days=400)
    for form, filed in zip(recent.get("form", []), recent.get("filingDate", []), strict=False):
        if form in _PERIODIC_FORMS and window <= pd.Timestamp(filed) <= cutoff + pd.Timedelta(days=120):
            return True
    return False


def resolve_cik(name: str, as_of: str) -> str | None:
    """The one filer this company name meant on ``as_of``, or None.

    Exact normalized name first, then the name without its legal-form tail.
    Candidates must have been filing 10-Ks or 10-Qs around the date. Zero or
    several survivors is None: a guessed filer would put another company's
    statements under this ticker.
    """
    exact, stems = _name_index()
    key = normalize_name(name)
    for candidates in (exact.get(key, set()), stems.get(_stem(key), set())):
        active = [cik for cik in sorted(candidates) if _was_filing(cik, as_of)]
        if len(active) == 1:
            return active[0]
        if len(active) > 1:
            logger.info("EDGAR: %r matches %d active filers; not guessing", name, len(active))
            return None
    return None


# --- facts -> statements ---------------------------------------------------------------


def _span(fact: dict) -> int:
    return (datetime.fromisoformat(fact["end"]) - datetime.fromisoformat(fact["start"])).days


def _known(units: list[dict], cutoff: str, duration: bool) -> dict:
    """Latest-filed value per period among facts filed on or before ``cutoff``.

    Keyed by (start, end) for flows and end for balances. Taking the latest
    filing up to the cutoff uses a restatement exactly from the day it was
    filed, and never before.
    """
    best: dict = {}
    for fact in units:
        if fact.get("form") not in _PERIODIC_FORMS or fact.get("filed", "9999") > cutoff:
            continue
        if duration and "start" not in fact:
            continue
        key = (fact["start"], fact["end"]) if duration else fact["end"]
        if key not in best or fact["filed"] >= best[key]["filed"]:
            best[key] = fact
    return {k: float(v["val"]) for k, v in best.items()}


def _merged(gaap: dict, tags: tuple[str, ...], unit: str, cutoff: str, duration: bool) -> dict:
    """Known values per period across a list of tags, the earliest-listed tag winning.

    Chosen per period, not per history: filers switch tags. Coca-Cola filed its
    long-term debt as LongTermDebtNoncurrent until March 2024 and as
    LongTermDebtAndCapitalLeaseObligations after; taking the first tag with any
    values would have lost every period since the switch.
    """
    out: dict = {}
    for tag in tags:
        values = _known(gaap.get(tag, {}).get("units", {}).get(unit, []), cutoff, duration)
        for key, val in values.items():
            out.setdefault(key, val)
    return out


def _cover_shares(facts: dict, ends: list[str], cutoff: str) -> dict:
    """Shares outstanding for each period, from the filing's cover page.

    Many filers (Coca-Cola among them) never tag a period-end share count; every
    filer states the count on its cover as of a date just after the period.
    Each period takes the first cover date within 130 days after its end.
    """
    dei = facts.get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    cover = sorted(_known(dei, cutoff, duration=False).items())
    out = {}
    for end in ends:
        e = datetime.fromisoformat(end)
        for date, val in cover:
            if 0 < (datetime.fromisoformat(date) - e).days <= 130:
                out[end] = val
                break
    return out


def _flows(values: dict) -> tuple[dict, dict]:
    """(annual by end date, single quarters by end date) from dated flows.

    10-Q cash flows are year-to-date: six and nine months, then the 10-K's
    twelve. A single quarter is the difference between consecutive
    year-to-date figures that share a start date, which also recovers the
    fourth quarter as the year less nine months. A directly reported
    three-month figure always wins over a derived one.
    """
    annual: dict = {}
    quarters: dict = {}
    by_start: dict = {}
    for (start, end), val in values.items():
        days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
        if _ANNUAL_DAYS[0] <= days <= _ANNUAL_DAYS[1]:
            annual[end] = val
        if _QUARTER_DAYS[0] <= days <= _QUARTER_DAYS[1]:
            quarters[end] = val
        by_start.setdefault(start, []).append((end, days, val))
    for chain in by_start.values():
        chain.sort()
        for (_e1, d1, v1), (e2, d2, v2) in zip(chain, chain[1:], strict=False):
            if _QUARTER_DAYS[0] <= d2 - d1 <= _QUARTER_DAYS[1] and e2 not in quarters:
                quarters[e2] = v2 - v1
    return annual, quarters


def edgar_financials(cik: str, ticker: str, as_of: str) -> Financials:
    """A :class:`Financials` built from EDGAR facts filed on or before ``as_of``."""
    facts = _sec_json(_FACTS_URL.format(cik=cik), f"companyfacts_{cik}.json").get("facts", {})
    gaap = facts.get("us-gaap", {})
    if not gaap:
        raise FinancialsUnavailable(f"EDGAR has no US-GAAP facts for CIK {cik} (an IFRS filer?)")

    annual_cols: dict[str, dict] = {}
    quarter_cols: dict[str, dict] = {}
    for field, tags in _DURATION.items():
        annual, quarters = _flows(_merged(gaap, tags, "USD", as_of, duration=True))
        annual_cols[field], quarter_cols[field] = annual, quarters

    ends_a = sorted(annual_cols["totalRevenue"] or annual_cols["netIncome"] or annual_cols["operatingCashflow"])
    ends_q = sorted(quarter_cols["totalRevenue"] or quarter_cols["netIncome"])

    def at(balances: dict, ends: list[str]) -> dict:
        return {e: balances[e] for e in ends if e in balances}

    for field, tags in _INSTANT.items():
        balances = _merged(gaap, tags, "USD", as_of, duration=False)
        annual_cols[field], quarter_cols[field] = at(balances, ends_a), at(balances, ends_q)

    groups = [_merged(gaap, g, "USD", as_of, duration=False) for g in _SHORT_TERM_DEBT_GROUPS]
    short = {e: sum(g[e] for g in groups if e in g) for e in set().union(*groups)}
    annual_cols["shortTermDebt"], quarter_cols["shortTermDebt"] = at(short, ends_a), at(short, ends_q)

    tagged = _merged(gaap, _SHARES_CURRENT, "shares", as_of, duration=False)
    for cols, ends in ((annual_cols, ends_a), (quarter_cols, ends_q)):
        shares = _cover_shares(facts, ends, as_of)
        shares.update(at(tagged, ends))  # a period-end tag, where filed, is exact
        cols["commonStockSharesOutstanding"] = shares

    # Alpha Vantage reports buybacks as negative "proceeds"; match its sign.
    for cols in (annual_cols, quarter_cols):
        cols["proceedsFromRepurchaseOfEquity"] = {
            e: -v for e, v in cols.pop("paymentsForRepurchaseOfCommonStock").items()
        }

    def frame(cols: dict, ends: list[str]) -> pd.DataFrame:
        index = pd.to_datetime(ends)
        data = {field: [series.get(e, float("nan")) for e in ends] for field, series in cols.items()}
        return pd.DataFrame(data, index=index).sort_index()

    annual = frame(annual_cols, ends_a)
    if annual.empty:
        raise FinancialsUnavailable(f"EDGAR has no annual statements for CIK {cik} filed by {as_of}")
    return Financials(ticker.strip().upper(), pd.Timestamp(as_of), "USD", annual, frame(quarter_cols, ends_q))


def load_delisted_financials(ticker: str, as_of: str) -> Financials:
    """Statements for a ticker that has since delisted, or FinancialsUnavailable."""
    name = delisted_name(ticker, as_of)
    if not name:
        raise FinancialsUnavailable(f"{ticker} is not a delisted US listing Alpha Vantage knows")
    cik = resolve_cik(name, as_of)
    if not cik:
        raise FinancialsUnavailable(f"no unique SEC filer for {name!r} around {as_of}")
    return edgar_financials(cik, ticker, as_of)
