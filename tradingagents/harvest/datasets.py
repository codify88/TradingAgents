"""What the harvest collects, how often, and when each fact became public.

The "public when" rules are the point-in-time half of the harvest: a historical
query as of date D must see only what could have been read on D.

- Insider trades: Alpha Vantage reports only the transaction date; a Form 4 is
  due within two business days, so a trade is public two business days later.
- Congressional trades: the disclosure's ``filed_date`` -- often weeks after the
  trade (traded 2026-08-20, filed 2026-09-17 in the first probe).
- Institutional holdings: 13Fs are due 45 days after the quarter they report.
- Transcripts: the call date, which is the quarter's ``EARNINGS`` report date.
- ETF holdings: the day the snapshot was taken.

Design: docs/design/llmquant.md, "Harvest".
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

VENDOR = "alpha_vantage"

INSIDER_LAG_BUSINESS_DAYS = 2
THIRTEEN_F_LAG = timedelta(days=45)

# Refresh cadences for the harvest's own schedule (the store's freshness is
# per New York date; the harvest refreshes less often than that).
WEEKLY = timedelta(days=7)
MONTHLY = timedelta(days=30)
QUARTERLY = timedelta(days=90)
# A transcript that came back empty is asked again at most this often until its
# quarter settles and the empty answer is stored as final.
TRANSCRIPT_RETRY = timedelta(days=7)

# The ETFs snapshotted weekly; the rest monthly (5,875 are listed).
WEEKLY_ETFS = 500

_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), start=1)}


def insider_public(transaction_date: str) -> str:
    d = pd.Timestamp(transaction_date) + pd.offsets.BDay(INSIDER_LAG_BUSINESS_DAYS)
    return d.date().isoformat()


def insider_cutoff(curr_date: str) -> str:
    """The latest transaction date that was public on ``curr_date``."""
    d = pd.Timestamp(curr_date) - pd.offsets.BDay(INSIDER_LAG_BUSINESS_DAYS)
    return d.date().isoformat()


def thirteen_f_public(period_end: str) -> str:
    return (date.fromisoformat(period_end) + THIRTEEN_F_LAG).isoformat()


def fiscal_year_end_month(name: str | None) -> int:
    """``OVERVIEW`` FiscalYearEnd ("September") -> 9; December when unknown."""
    return _MONTHS.get((name or "").strip().lower(), 12)


def fiscal_quarter(fiscal_date_ending: str, fye_month: int) -> str:
    """The vendor's fiscal-quarter label for a quarter ending on ``fiscal_date_ending``.

    Fiscal years are named for the calendar year they end in, and quarter one
    starts the month after the fiscal year-end. Checked against Alpha Vantage on
    Apple (FYE September): the quarter ending 2024-12-31 is "2025Q1", the one
    ending 2024-09-30 is "2024Q4". A 52/53-week year can end a few days into the
    next month; a date in the first week is read as the month before.
    """
    d = date.fromisoformat(fiscal_date_ending)
    if d.day <= 7:  # 52/53-week year ending early in a month belongs to the prior one
        d = d.replace(day=1) - timedelta(days=1)
    fiscal_year = d.year if d.month <= fye_month else d.year + 1
    quarter = ((d.month - fye_month - 1) % 12) // 3 + 1
    return f"{fiscal_year}Q{quarter}"
