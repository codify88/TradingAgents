"""Tier 0: the investable universe, before any market data is fetched.

Alpha Vantage's LISTING_STATUS is one keyless-per-run CSV covering every active
US listing. Filtering here is deliberately crude and cheap -- exchange, asset
type, listing age -- because everything downstream costs either a price
download or an API call per name.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

import pandas as pd

from tradingagents.dataflows.alpha_vantage_common import _make_api_request

# Venues whose listings have the disclosure and liquidity regime this is built
# for. OTC is excluded at the universe level rather than by a later liquidity
# filter: the data quality problem is categorical, not a matter of degree.
DEFAULT_EXCHANGES = ("NYSE", "NASDAQ", "AMEX", "NYSE MKT", "BATS")

# A company needs enough reported history for the mandates' screens to mean
# anything -- quality metrics read a decade, momentum reads a year.
MIN_LISTING_AGE_DAYS = 400


@dataclass(frozen=True)
class Candidate:
    """One symbol under consideration, with why it survived or did not."""

    symbol: str
    name: str
    exchange: str
    ipo_date: str

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

    ``limit`` truncates deterministically (alphabetically) rather than randomly,
    so a capped run is reproducible; it exists for cheap end-to-end runs, not
    as a sampling strategy.
    """
    cutoff = pd.Timestamp(as_of) - pd.Timedelta(days=min_age_days)
    allowed = {e.upper() for e in exchanges}

    out = []
    for row in _rows(_make_api_request("LISTING_STATUS", {})):
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
        ))

    out.sort(key=lambda c: c.symbol)
    return out[:limit] if limit else out
