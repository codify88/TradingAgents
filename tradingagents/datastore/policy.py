"""How long a stored vendor response can be served, per endpoint.

The rule that saves the calls: **a response about a closed past period is
final** -- a 2019 option chain, a 2020 listing, last year's news -- and is kept
forever (the Alpha Vantage terms allow a permanent local store; decided
2026-09-25). Everything else is served only on the New York trading date it was
fetched, and during market hours for at most fifteen minutes, because the day's
bar is still moving. A few endpoints keep no history on the vendor side; their
responses are also appended as dated snapshots, which is the only way that
history comes to exist.

Point-in-time correctness is not this module's job: the tools cut every series
at the analysis date. The store only remembers what the vendor said, and when.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
INTRADAY_MAX_AGE = timedelta(minutes=15)
_MARKET_OPEN, _MARKET_CLOSE = time(9, 30), time(16, 30)

# Endpoints whose vendor keeps no history: each distinct response is kept.
SNAPSHOT_ENDPOINTS = frozenset({"INSTITUTIONAL_HOLDINGS", "ETF_PROFILE"})

# Recent news is still being indexed; a window that closed a week ago is settled.
NEWS_SETTLE = timedelta(days=7)
NEWS_RECENT_MAX_AGE = timedelta(hours=1)

# A quarter's earnings call happens within weeks of the quarter end. Four months
# on, the transcript the vendor has is the one it will keep.
TRANSCRIPT_SETTLE = timedelta(days=120)

WEEKLY = timedelta(days=7)


@dataclass(frozen=True)
class Policy:
    final: bool                      # serve forever, never refetch
    max_age: timedelta | None = None  # None with final=False: same New York trading date
    snapshot: bool = False           # also append to the snapshot history


def _day(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _quarter_end(label: str | None) -> date | None:
    """``2024Q1`` -> 2024-03-31. Fiscal labels are treated as calendar quarters
    here, which only shifts the settle point by a quarter at most."""
    if not label or len(label) != 6 or label[4] not in "Qq":
        return None
    try:
        year, q = int(label[:4]), int(label[5])
    except ValueError:
        return None
    if q not in (1, 2, 3, 4):
        return None
    month = q * 3
    return date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)


def policy_for(endpoint: str, params: dict, now: datetime) -> Policy:
    today = now.astimezone(NEW_YORK).date()
    snapshot = endpoint in SNAPSHOT_ENDPOINTS

    if endpoint in ("HISTORICAL_OPTIONS", "LISTING_STATUS"):
        d = _day(params.get("date"))
        return Policy(final=d is not None and d < today)

    if endpoint == "NEWS_SENTIMENT":
        end = _day(params.get("time_to"))
        if end is not None and today - end > NEWS_SETTLE:
            return Policy(final=True)
        return Policy(final=False, max_age=NEWS_RECENT_MAX_AGE)

    if endpoint == "EARNINGS_CALL_TRANSCRIPT":
        q_end = _quarter_end(params.get("quarter"))
        return Policy(final=q_end is not None and today - q_end > TRANSCRIPT_SETTLE)

    if endpoint == "POLITICIAN_METADATA":
        return Policy(final=False, max_age=WEEKLY)

    return Policy(final=False, snapshot=snapshot)


def is_fresh(policy: Policy, fetched_at: datetime, now: datetime) -> bool:
    if policy.final:
        return True
    age = now - fetched_at
    if policy.max_age is not None:
        return age <= policy.max_age
    fetched_ny, now_ny = fetched_at.astimezone(NEW_YORK), now.astimezone(NEW_YORK)
    if fetched_ny.date() != now_ny.date():
        return False
    # Fetched while the session was open: the day's bar was still moving.
    during_session = (fetched_ny.weekday() < 5
                      and _MARKET_OPEN <= fetched_ny.time() < _MARKET_CLOSE)
    return age <= INTRADAY_MAX_AGE if during_session else True
