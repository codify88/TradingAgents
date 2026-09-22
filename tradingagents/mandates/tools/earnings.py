"""When the next earnings report lands, and what the street expects from it.

The momentum mandate tells the portfolio manager to treat the next print as the
interim kill-check, and the Growth Analyst to weigh revision breadth against the
catalyst that will settle it. Neither could date that catalyst: the system
reasoned about "the next earnings print" without knowing when it was.

Alpha Vantage's EARNINGS_CALENDAR covers the whole market in one request, so
this is one call per horizon rather than one per name.
"""

from __future__ import annotations

import csv
import functools
import io
from dataclasses import dataclass

import pandas as pd

from .financials import _make_api_request
from .growth import ESTIMATE_STALENESS_TOLERANCE_DAYS

HORIZON = "3month"


@dataclass(frozen=True)
class EarningsDate:
    """One scheduled report."""

    symbol: str
    name: str
    report_date: pd.Timestamp
    fiscal_period_end: pd.Timestamp | None
    estimate: float | None
    currency: str

    def days_away(self, as_of: pd.Timestamp) -> int:
        return (self.report_date.normalize() - as_of.normalize()).days


@functools.lru_cache(maxsize=4)
def _calendar_csv(horizon: str) -> str:
    """The whole market's calendar, cached for the life of the process.

    Every name in a screen's shortlist asks the same question, and the vendor
    answers it for all of them at once.
    """
    return _make_api_request("EARNINGS_CALENDAR", {"horizon": horizon})


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value: str) -> pd.Timestamp | None:
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(stamp) else stamp


def load_calendar(horizon: str = HORIZON) -> dict[str, list[EarningsDate]]:
    """Scheduled reports by symbol, soonest first.

    Parsed with csv, not split on commas: company names carry them
    ("CARLYLE GROUP L.P."), and a naive split silently shifts every later
    column -- the report date becomes part of a company name.
    """
    out: dict[str, list[EarningsDate]] = {}
    for row in csv.DictReader(io.StringIO(_calendar_csv(horizon))):
        symbol = (row.get("symbol") or "").strip().upper()
        report = _date(row.get("reportDate", ""))
        if not symbol or report is None:
            continue
        out.setdefault(symbol, []).append(EarningsDate(
            symbol=symbol,
            name=(row.get("name") or "").strip(),
            report_date=report,
            fiscal_period_end=_date(row.get("fiscalDateEnding", "")),
            estimate=_float(row.get("estimate", "")),
            currency=(row.get("currency") or "").strip() or "USD",
        ))
    for dates in out.values():
        dates.sort(key=lambda d: d.report_date)
    return out


def next_earnings(ticker: str, as_of: pd.Timestamp,
                  horizon: str = HORIZON) -> EarningsDate | None:
    """The next scheduled report at or after ``as_of``, or None.

    The calendar describes the schedule as it stands today and carries no as-of
    date, so a genuinely historical run is not served it -- the same rule the
    estimate revisions follow, and for the same reason: a backtest that knows
    the future earnings calendar knows something the decision could not have.
    """
    age = (pd.Timestamp.today().normalize() - as_of.normalize()).days
    if age > ESTIMATE_STALENESS_TOLERANCE_DAYS:
        return None
    upcoming = [
        d for d in load_calendar(horizon).get(ticker.strip().upper(), [])
        if d.report_date.normalize() >= as_of.normalize()
    ]
    return upcoming[0] if upcoming else None
