"""The tiers: narrow by exclusion, order by one declared signal, keep a control.

Tier 0  universe          ~8,600 -> ~4,000   no market data
Tier 1  price exclusions  ~4,000 ->   ~300   one batched download per 200 names
Tier 2  fundamentals      ~300   ->    ~30   one API call per surviving name
Tier 3  the agent loop           ->   5-10   ~30 LLM calls each

Only tiers 0-2 live here. Tier 3 is the existing graph, which this hands a list.
"""

from __future__ import annotations

import logging
import math
import random
from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

from tradingagents.mandates import Mandate, get_mandate
from tradingagents.mandates.tools import growth as gr, momentum as mo, valuation as val
from tradingagents.mandates.tools.financials import (
    FinancialsUnavailable,
    load_financials,
    price_history,
)
from tradingagents.mandates.tools.quality import annual_metrics, quality_screens

from . import prices
from .manifest import ScreenManifest, make_run_id
from .throttle import DEFAULT_REQUESTS_PER_MINUTE, RateLimiter, with_retry
from .universe import load_universe

logger = logging.getLogger(__name__)

# Investability, not thesis: these apply whatever the mandate, and exist so the
# loop is never spent on something that cannot be traded or cannot be measured.
MIN_DOLLAR_VOLUME = 5_000_000.0
MIN_PRICE = 5.0
MIN_HISTORY_BARS = 252

# How many survivors pay for a fundamentals call. The cut into tier 2 is made on
# liquidity -- deliberately neutral -- so a mandate's own signal is not applied
# twice, once to choose who gets examined and again to rank the results.
DEFAULT_FUNDAMENTAL_BUDGET = 60
DEFAULT_PICKS = 8
DEFAULT_CONTROLS = 3

BENCHMARK = "SPY"

# Above this share of the universe unreachable, a shortlist is not a screen: it
# is whatever leaked through an outage. The run still reports its funnel, so the
# failure is visible, but it refuses to name picks.
MAX_UNAVAILABLE_FRACTION = 0.25


@dataclass
class TierStat:
    """What one tier did, and why it dropped what it dropped."""

    name: str
    examined: int
    kept: int
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def dropped(self) -> int:
        return self.examined - self.kept


@dataclass
class ScreenResult:
    manifest: ScreenManifest
    eligible: list[str]
    excluded: dict[str, list[str]]   # symbol -> rules it tripped
    usable: bool = True              # False when the vendor failed too widely


# --- exclusions -------------------------------------------------------------------


def investability_exclusions(frame: pd.DataFrame) -> list[str]:
    """Reasons a name cannot be traded or cannot be judged, regardless of mandate."""
    out = []
    if len(frame) < MIN_HISTORY_BARS:
        out.append("insufficient price history")
    if frame.empty:
        return out
    price = float(frame["Close"].iloc[-1])
    if math.isnan(price) or price < MIN_PRICE:
        out.append(f"price below ${MIN_PRICE:.0f}")
    dv = prices.dollar_volume(frame)
    if math.isnan(dv) or dv < MIN_DOLLAR_VOLUME:
        out.append(f"median dollar volume below ${MIN_DOLLAR_VOLUME:,.0f}")
    return out


def price_exclusions(mandate: Mandate | None, frame: pd.DataFrame, bench: pd.DataFrame) -> list[str]:
    """Mandate disqualifiers that price alone can settle.

    Only TRIPPED counts. A WATCH is exactly the kind of contestable call the
    analyst debate exists to make, and NO DATA is not evidence of anything --
    excluding on either would quietly narrow the universe on the screener's
    judgement rather than on the mandate's rules.
    """
    if mandate is None or mandate.name != "equity_momentum" or frame.empty:
        return []
    try:
        structure = mo.trend_structure(frame)
        trailing = mo.trailing_returns(frame["Close"])
        vs_bench = mo.excess_returns(frame["Close"], bench["Close"]) if not bench.empty else {}
        return [s.name for s in mo.momentum_screens(structure, trailing, vs_bench)
                if s.status == "TRIPPED"]
    except Exception as exc:
        logger.debug("price screens failed: %s", exc)
        return []


def fundamental_exclusions(mandate: Mandate | None, symbol: str, as_of: str,
                           frame: pd.DataFrame,
                           limiter: RateLimiter | None = None) -> tuple[list[str], float]:
    """Mandate disqualifiers the statements can settle, plus the ordering value.

    Returns ``(tripped rules, ordering value)``. A vendor failure excludes the
    name with a stated reason rather than passing it through unexamined: an
    unverifiable candidate is not the same as an eligible one.
    """
    if mandate is None:
        return [], float("nan")
    if limiter is not None:
        # Income statement, balance sheet, cash flow and the earnings calendar.
        limiter.acquire(4)
    try:
        fin = with_retry(lambda: load_financials(symbol, as_of))
    except FinancialsUnavailable as exc:
        return [f"fundamentals unavailable ({exc})"], float("nan")
    except Exception as exc:
        return [f"fundamentals unavailable ({type(exc).__name__})"], float("nan")

    try:
        if mandate.name == "equity_value":
            metrics = annual_metrics(fin)
            tripped = [s.name for s in quality_screens(metrics) if s.status == "TRIPPED"]
            return tripped, _value_ordering(fin, frame)

        if mandate.name == "equity_momentum":
            trajectory = gr.growth_trajectory(fin)
            price_12m = mo.trailing_returns(frame["Close"])["12m"] if not frame.empty else float("nan")
            eps = trajectory.annual_eps_growth.dropna()
            expanding = None if (math.isnan(price_12m) or eps.empty) else bool(price_12m > float(eps.iloc[-1]))
            tripped = [s.name for s in gr.growth_screens(trajectory, [], expanding, price_12m)
                       if s.status == "TRIPPED"]
            return tripped, float("nan")  # momentum orders on tier-1 data
    except Exception as exc:
        return [f"screens could not be computed ({type(exc).__name__})"], float("nan")
    return [], float("nan")


# --- ordering ---------------------------------------------------------------------
# Deliberately one number per mandate, named in the manifest, so whatever bias
# the ordering introduces is visible rather than buried in a composite score.

VALUE_ORDERING = "FCF yield percentile against the company's own ten-year history (high = cheap vs itself)"
MOMENTUM_ORDERING = "12-month excess total return over SPY"


def _long_prices(fin) -> pd.Series:
    """Prices back to the first fiscal year on file.

    The tier-1 download spans about eighteen months, which is all momentum's
    windows need. A valuation percentile is against ten fiscal year ends, so it
    needs its own history -- fetched only for the handful of names that reach
    the fundamental tier, through the same cached helper the Valuation Analyst
    uses, so the screen's ordering and the analyst's report agree.
    """
    if fin.annual.empty:
        return pd.Series(dtype=float)
    return price_history(fin.ticker, fin.annual.index[0] - pd.Timedelta(days=10), fin.as_of)


def _value_ordering(fin, frame: pd.DataFrame) -> float:
    """Where today's FCF yield sits in the company's own ten-year range.

    Against its own history, not against other companies: a cross-sectional
    cheapness rank would mostly sort by sector, and this mandate's question is
    whether *this* business is cheap relative to how it has been priced.
    """
    try:
        closes = _long_prices(fin)
        current = val.snapshot(fin, closes)
        history = val.historical_multiples(fin, closes)
        if current is None or history.empty or "fcf_yield" not in history:
            return float("nan")
        if math.isnan(current.market_cap) or current.market_cap <= 0:
            return float("nan")
        today = current.ttm_fcf / current.market_cap
        return val.percentile_in_history(today, history["fcf_yield"])
    except Exception:
        return float("nan")


def _momentum_ordering(frame: pd.DataFrame, bench: pd.DataFrame) -> float:
    if frame.empty or bench.empty:
        return float("nan")
    try:
        return mo.excess_returns(frame["Close"], bench["Close"])["12m"]
    except Exception:
        return float("nan")


# --- the run ----------------------------------------------------------------------


def run_screen(
    mandate_name: str,
    as_of: str,
    config: dict,
    picks: int = DEFAULT_PICKS,
    controls: int = DEFAULT_CONTROLS,
    fundamental_budget: int = DEFAULT_FUNDAMENTAL_BUDGET,
    universe_limit: int | None = None,
    control_seed: int | None = None,
    requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
) -> ScreenResult:
    """Narrow the universe to a shortlist, with a random control drawn alongside."""
    mandate = get_mandate(mandate_name)
    as_of_ts = pd.Timestamp(as_of)
    start = (as_of_ts - pd.Timedelta(days=int(365 * 1.6))).strftime("%Y-%m-%d")
    end = (as_of_ts + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    tiers: list[TierStat] = []
    excluded: dict[str, list[str]] = {}

    universe = load_universe(as_of, limit=universe_limit)
    tiers.append(TierStat("universe", examined=len(universe), kept=len(universe)))
    delisted = {c.symbol for c in universe if c.delisted_since}

    # One limiter for every Alpha Vantage call the screen makes: the price
    # fallback for delisted names and the fundamentals tier share one budget.
    limiter = RateLimiter(requests_per_minute)
    price_data = prices.download(
        [c.symbol for c in universe], start, end, fallback=delisted, limiter=limiter,
    )
    frames = price_data.frames
    bench = prices.download([BENCHMARK], start, end).frames.get(BENCHMARK, pd.DataFrame())

    # --- tier 1: price ---
    reasons: Counter = Counter()
    survivors = []
    for candidate in universe:
        frame = frames.get(candidate.symbol)
        if frame is None:
            reason = (
                "price data unavailable (vendor failed, not a fact about the company)"
                if candidate.symbol in price_data.unavailable
                else "no price history"
            )
            excluded[candidate.symbol] = [reason]
            reasons[reason] += 1
            continue
        rules = investability_exclusions(frame) + price_exclusions(mandate, frame, bench)
        if rules:
            excluded[candidate.symbol] = rules
            reasons[rules[0]] += 1
            continue
        survivors.append(candidate.symbol)
    tiers.append(TierStat("price", len(universe), len(survivors), dict(reasons)))

    # A widespread vendor failure is not a screening result, so stop before the
    # fundamentals budget is spent on a sample of the vendor's mood.
    usable = price_data.failure_rate <= MAX_UNAVAILABLE_FRACTION
    if not usable:
        survivors = []

    # The cut into the expensive tier is by liquidity, which is neutral to both
    # mandates: ordering here on a mandate's own signal would apply it twice.
    survivors.sort(key=lambda s: prices.dollar_volume(frames[s]), reverse=True)
    examined = survivors[:fundamental_budget]
    if len(survivors) > len(examined):
        tiers.append(TierStat(
            "liquidity budget", len(survivors), len(examined),
            {f"outside the {fundamental_budget} most liquid survivors "
             f"(not a judgement on the name)": len(survivors) - len(examined)},
        ))

    # --- tier 2: fundamentals ---
    reasons = Counter()
    eligible: list[str] = []
    ordering: dict[str, float] = {}
    lost_to_statements: list[str] = []
    for symbol in examined:
        rules, value = fundamental_exclusions(mandate, symbol, as_of, frames[symbol], limiter)
        if rules and symbol in delisted and rules[0].startswith("fundamentals unavailable"):
            # Say what this is: not a fact about the company, but the vendor
            # keeping no statements once a company delists.
            rules = [DELISTED_NO_STATEMENTS]
            lost_to_statements.append(symbol)
        if rules:
            excluded[symbol] = rules
            reasons[rules[0]] += 1
            continue
        eligible.append(symbol)
        ordering[symbol] = (
            value if mandate and mandate.name == "equity_value"
            else _momentum_ordering(frames[symbol], bench)
        )
    tiers.append(TierStat("fundamental", len(examined), len(eligible), dict(reasons)))

    # --- ordering, then a control drawn from what the ordering rejected ---
    ranked = sorted(
        eligible,
        key=lambda s: (-1e18 if math.isnan(ordering.get(s, float("nan"))) else ordering[s]),
        reverse=True,
    )
    chosen = ranked[:picks]
    # Drawn from the eligible names the ranking did *not* pick, so the two
    # groups are disjoint and the comparison is between "ranked highest" and
    # "eligible but not ranked highest" -- not between overlapping sets.
    pool = list(ranked[picks:])
    seed = control_seed if control_seed is not None else random.randrange(2**31)
    control = random.Random(seed).sample(pool, min(controls, len(pool)))

    signal = VALUE_ORDERING if (mandate and mandate.name == "equity_value") else MOMENTUM_ORDERING
    notes = []
    if delisted:
        notes.append(survivorship_note(delisted, survivors, examined, lost_to_statements, eligible))

    if not usable:
        # The funnel is still reported, so the failure is visible rather than
        # silently shaping a shortlist.
        notes.append(
            f"NO SHORTLIST: the price vendor could not answer for "
            f"{len(price_data.unavailable):,} of {len(universe):,} names "
            f"({price_data.failure_rate:.0%}), above the {MAX_UNAVAILABLE_FRACTION:.0%} "
            f"limit. That is an outage, not a finding about those companies, and "
            f"any shortlist drawn from what got through would be a sample of the "
            f"vendor's mood. Nothing further was run. Re-run when the vendor recovers."
        )
    else:
        if not pool:
            notes.append(
                "No control group: the eligible pool was not larger than the pick "
                "count, so there were no un-picked eligible names to draw from. "
                "Widen the fundamental budget or reduce picks to restore it."
            )
        if len(eligible) < picks:
            notes.append(
                f"Only {len(eligible)} names cleared every exclusion, fewer than the "
                f"{picks} requested; the shortlist is the whole eligible pool and "
                f"the ordering did no work."
            )

    manifest = ScreenManifest(
        run_id=make_run_id(mandate_name, as_of),
        mandate=mandate_name,
        as_of=as_of,
        created=pd.Timestamp.now().isoformat(timespec="seconds"),
        universe_size=len(universe),
        tiers=[{"name": t.name, "examined": t.examined, "kept": t.kept,
                "dropped": t.dropped, "reasons": t.reasons} for t in tiers],
        ordering_signal=signal,
        picks=[{"symbol": s, "rank": i + 1, "value": _safe(ordering.get(s))}
               for i, s in enumerate(chosen)],
        controls=[{"symbol": s, "value": _safe(ordering.get(s))} for s in control],
        control_seed=seed,
        eligible_count=len(eligible),
        notes=notes,
    )
    return ScreenResult(
        manifest=manifest, eligible=eligible, excluded=excluded, usable=usable,
    )


DELISTED_NO_STATEMENTS = (
    "delisted since the screen date; Alpha Vantage keeps no statements for "
    "delisted companies, so the fundamental screens cannot run"
)


def survivorship_note(delisted: set[str], price_survivors: list[str],
                      examined: list[str], lost: list[str], eligible: list[str]) -> str:
    """How far the names that have since delisted got, stated in the report.

    The universe and price tiers now see them; the fundamentals tier cannot,
    because the statements vendor drops a company when it delists. Rather than
    let that quietly re-create a survivors-only shortlist, the loss is counted
    here so the reader can judge how much it matters for this screen.
    """
    past_price = len(delisted & set(price_survivors))
    reached = len(delisted & set(examined))
    kept = len(delisted & set(eligible))
    return (
        f"Survivorship: {len(delisted):,} names in this universe have delisted since "
        f"the screen date. {past_price:,} passed the price tier, {reached:,} reached "
        f"the fundamentals tier, and {len(lost):,} of those were excluded there only "
        f"because no statements survive for delisted companies; {kept:,} remain "
        f"eligible. The universe and price tiers are free of survivorship bias; the "
        f"fundamentals tier is not, by that count."
    )


def _safe(x) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)
