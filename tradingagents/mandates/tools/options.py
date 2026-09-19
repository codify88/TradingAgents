"""Point-in-time option chains and our own option pricing, for LEAPS.

Alpha Vantage's HISTORICAL_OPTIONS returns a symbol's whole chain as it stood
on a date, with bid/ask, open interest, and vendor-computed implied volatility
and greeks. The quotes are usable; the vendor IV and greeks are not: on
low-volatility names 20-60% of long-dated calls come back with an IV under 5%
and delta 1.00 near the money (KO 2024-03-01: a 55 call on a ~$60 stock at IV
1.5%). So nothing here reads the vendor's IV, greeks or ``last`` (which is
often stale). IV and delta are recomputed from the bid/ask mid with
Black-Scholes-Merton, using the 2-year Treasury yield and the trailing
dividend yield, both as of the chain's date.

Everything is in the raw, unsplit terms of the date: strikes are listed that
way, dividends are reported that way (KO paid 0.51 before its 2012 split and
0.255 after), so the underlying price must be the raw close too.

See docs/design/leaps.md.
"""

from __future__ import annotations

import functools
import json
import math
from dataclasses import dataclass

import pandas as pd

from tradingagents.dataflows.alpha_vantage_common import _make_api_request

# LEAPS are long-dated; the 2-year yield is the closest listed maturity.
RATE_MATURITY = "2year"
TRADING_DAYS_PER_YEAR = 252
# How far past the holding horizon a contract must run, so the exit is not
# forced into the last months of its life, where time decay is steepest.
EXPIRY_BUFFER_TRADING_DAYS = 63
# A contract is liquid enough to trade and to grade when both hold: enough
# open interest that the quote is real, and a spread the round trip can bear.
MIN_OPEN_INTEREST = 100
MAX_SPREAD = 0.10

_VOL_LOW, _VOL_HIGH = 0.005, 5.0


# --- Black-Scholes-Merton ---------------------------------------------------------


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1(s: float, k: float, t: float, r: float, q: float, vol: float) -> float:
    return (math.log(s / k) + (r - q + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))


def bsm_call(s: float, k: float, t: float, r: float, q: float, vol: float) -> float:
    """European call value with a continuous dividend yield ``q``.

    LEAPS on US stocks are American, and a deep in-the-money call on a dividend
    payer can be worth exercising early; for the long-dated, mostly
    out-of-dividend-range contracts this module prices, the European value is
    the standard approximation and the error is small against the spread.
    """
    if t <= 0:
        return max(s - k, 0.0)
    d1 = _d1(s, k, t, r, q, vol)
    d2 = d1 - vol * math.sqrt(t)
    return s * math.exp(-q * t) * _norm_cdf(d1) - k * math.exp(-r * t) * _norm_cdf(d2)


def bsm_call_delta(s: float, k: float, t: float, r: float, q: float, vol: float) -> float:
    if t <= 0:
        return 1.0 if s > k else 0.0
    return math.exp(-q * t) * _norm_cdf(_d1(s, k, t, r, q, vol))


def bsm_put(s: float, k: float, t: float, r: float, q: float, vol: float) -> float:
    """European put value, by put-call parity.

    American puts carry more early-exercise value than calls -- a deep
    in-the-money put is worth exercising to collect the strike's interest -- so
    this European value slightly understates them, and an IV backed out of an
    American put quote reads slightly high. At the deltas and tenors LEAPS
    grading uses the effect is small against the spread.
    """
    if t <= 0:
        return max(k - s, 0.0)
    return bsm_call(s, k, t, r, q, vol) - s * math.exp(-q * t) + k * math.exp(-r * t)


def bsm_put_delta(s: float, k: float, t: float, r: float, q: float, vol: float) -> float:
    if t <= 0:
        return -1.0 if s < k else 0.0
    return bsm_call_delta(s, k, t, r, q, vol) - math.exp(-q * t)


def bsm_price(kind: str, s: float, k: float, t: float, r: float, q: float, vol: float) -> float:
    return (bsm_put if kind == "put" else bsm_call)(s, k, t, r, q, vol)


def bsm_delta(kind: str, s: float, k: float, t: float, r: float, q: float, vol: float) -> float:
    return (bsm_put_delta if kind == "put" else bsm_call_delta)(s, k, t, r, q, vol)


def implied_vol(price: float, s: float, k: float, t: float, r: float, q: float,
                kind: str = "call") -> float:
    """The volatility at which the option's value equals ``price``, or NaN.

    NaN when the price sits outside the no-arbitrage band -- below discounted
    intrinsic value, or above the discounted stock (a call) or strike (a put) --
    because no volatility produces it, and a number forced out of that band
    would look like a reading when it is a quote problem.
    """
    if t <= 0 or price <= 0 or s <= 0 or k <= 0:
        return float("nan")
    if kind == "put":
        lower = max(k * math.exp(-r * t) - s * math.exp(-q * t), 0.0)
        upper = k * math.exp(-r * t)
    else:
        lower = max(s * math.exp(-q * t) - k * math.exp(-r * t), 0.0)
        upper = s * math.exp(-q * t)
    if not lower < price < upper:
        return float("nan")
    lo, hi = _VOL_LOW, _VOL_HIGH
    if bsm_price(kind, s, k, t, r, q, hi) < price:
        return float("nan")
    for _ in range(100):
        mid = (lo + hi) / 2
        if bsm_price(kind, s, k, t, r, q, mid) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-6:
            break
    return (lo + hi) / 2


# --- data ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Contract:
    """One listed option as quoted on the chain's date."""

    contract_id: str
    expiry: pd.Timestamp
    strike: float
    kind: str               # "call" or "put"
    bid: float
    ask: float
    open_interest: int
    volume: int

    @property
    def two_sided(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.two_sided else float("nan")

    @property
    def spread(self) -> float:
        """Bid-ask spread as a fraction of the mid: the cost of one round trip's half."""
        return (self.ask - self.bid) / self.mid if self.two_sided else float("nan")


def _num(value, cast=float):
    try:
        return cast(float(value))
    except (TypeError, ValueError):
        return cast(0)


def with_retry(fn):
    """The screener's rate-limit retry, imported late: tradingagents.screener
    imports the mandates package, which imports this module."""
    from tradingagents.screener.throttle import with_retry as retry

    return retry(fn)


def _parse_chain(payload, as_of: pd.Timestamp) -> list[Contract]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    contracts = []
    for row in payload.get("data", []):
        # The chain must be the one quoted on or before the as-of date; a row
        # from a later day would be the future leaking into the backtest.
        if pd.Timestamp(row.get("date", as_of)) > as_of:
            continue
        contracts.append(Contract(
            contract_id=row["contractID"],
            expiry=pd.Timestamp(row["expiration"]),
            strike=_num(row["strike"]),
            kind=row["type"],
            bid=_num(row.get("bid")),
            ask=_num(row.get("ask")),
            open_interest=_num(row.get("open_interest"), int),
            volume=_num(row.get("volume"), int),
        ))
    return contracts


@functools.lru_cache(maxsize=256)
def option_chain(symbol: str, date: str) -> tuple[Contract, ...]:
    """Every contract listed for ``symbol`` as quoted on ``date``."""
    payload = with_retry(lambda: _make_api_request(
        "HISTORICAL_OPTIONS", {"symbol": symbol, "date": date}))
    return tuple(_parse_chain(payload, pd.Timestamp(date)))


@functools.lru_cache(maxsize=1)
def _treasury_series(maturity: str) -> pd.Series:
    payload = with_retry(lambda: _make_api_request(
        "TREASURY_YIELD", {"interval": "daily", "maturity": maturity}))
    if isinstance(payload, str):
        payload = json.loads(payload)
    rows = {
        pd.Timestamp(r["date"]): float(r["value"])
        for r in payload.get("data", []) if r.get("value") not in (None, "", ".")
    }
    return pd.Series(rows, dtype=float).sort_index() / 100


def risk_free_rate(date: str) -> float:
    """The 2-year Treasury yield last published on or before ``date``, as a fraction."""
    s = _treasury_series(RATE_MATURITY).loc[:pd.Timestamp(date)]
    return float(s.iloc[-1]) if len(s) else float("nan")


@functools.lru_cache(maxsize=256)
def _dividends(symbol: str) -> pd.Series:
    payload = with_retry(lambda: _make_api_request("DIVIDENDS", {"symbol": symbol}))
    if isinstance(payload, str):
        payload = json.loads(payload)
    rows = {}
    for r in payload.get("data", []):
        try:
            rows[pd.Timestamp(r["ex_dividend_date"])] = float(r["amount"])
        except (KeyError, TypeError, ValueError):
            continue
    return pd.Series(rows, dtype=float).sort_index()


def dividend_yield(symbol: str, date: str, price: float) -> float:
    """Trailing-year dividends over the raw price, as a continuous-yield input.

    Only dividends that went ex on or before ``date`` count, so a backtest
    never prices in a payout that had not been declared.
    """
    if not price or price <= 0:
        return float("nan")
    end = pd.Timestamp(date)
    d = _dividends(symbol)
    paid = d[(d.index > end - pd.Timedelta(days=365)) & (d.index <= end)].sum()
    return float(paid / price)


# --- pricing a contract -------------------------------------------------------------


@dataclass(frozen=True)
class PricedContract:
    contract: Contract
    underlying: float
    years: float
    rate: float
    dividend_yield: float
    iv: float
    delta: float

    @property
    def intrinsic(self) -> float:
        k, s = self.contract.strike, self.underlying
        return max(k - s, 0.0) if self.contract.kind == "put" else max(s - k, 0.0)

    @property
    def extrinsic(self) -> float:
        """Time value paid at the ask: what the option costs beyond exercising now."""
        return self.contract.ask - self.intrinsic

    @property
    def breakeven_move(self) -> float:
        """The stock move to expiry at which the option bought at the ask breaks even
        (negative for a put: the fall it needs)."""
        k, ask = self.contract.strike, self.contract.ask
        if self.contract.kind == "put":
            return (k - ask) / self.underlying - 1
        return (k + ask) / self.underlying - 1

    @property
    def leverage(self) -> float:
        """Percent change in the option per 1% move in the stock, at the mid,
        in the direction the option profits from."""
        return abs(self.delta) * self.underlying / self.contract.mid


def price_contract(
    contract: Contract, underlying: float, date: str, rate: float, q: float,
) -> PricedContract:
    years = (contract.expiry - pd.Timestamp(date)).days / 365.0
    iv = implied_vol(contract.mid, underlying, contract.strike, years, rate, q, contract.kind)
    delta = (bsm_delta(contract.kind, underlying, contract.strike, years, rate, q, iv)
             if not math.isnan(iv) else float("nan"))
    return PricedContract(contract, underlying, years, rate, q, iv, delta)


def select_call(
    chain, underlying: float, date: str, rate: float, q: float,
    horizon_days: int, target_delta: float,
) -> PricedContract | None:
    """The call the LEAPS rule picks, or None when nothing qualifies (see :func:`select_option`)."""
    return select_option(chain, underlying, date, rate, q, horizon_days, target_delta, "call")


def select_put(
    chain, underlying: float, date: str, rate: float, q: float,
    horizon_days: int, target_delta: float,
) -> PricedContract | None:
    """The put the rule picks; ``target_delta`` is the magnitude (0.75 means -0.75)."""
    return select_option(chain, underlying, date, rate, q, horizon_days, target_delta, "put")


def select_option(
    chain, underlying: float, date: str, rate: float, q: float,
    horizon_days: int, target_delta: float, kind: str,
) -> PricedContract | None:
    """The contract the LEAPS rule picks, or None when nothing qualifies.

    The shortest expiry that still runs ``EXPIRY_BUFFER_TRADING_DAYS`` past the
    holding horizon, then the two-sided contract of ``kind`` whose computed
    delta is nearest the target in magnitude. A rule rather than a judgement, so
    every graded LEAPS decision is reproducible from the chain alone.
    """
    need = pd.Timestamp(date) + pd.Timedelta(
        days=math.ceil((horizon_days + EXPIRY_BUFFER_TRADING_DAYS) * 365 / TRADING_DAYS_PER_YEAR))
    candidates = [c for c in chain if c.kind == kind and c.two_sided and c.expiry >= need]
    picks = []
    for expiry in sorted({c.expiry for c in candidates}):
        priced = [price_contract(c, underlying, date, rate, q) for c in candidates if c.expiry == expiry]
        priced = [p for p in priced if not math.isnan(p.delta)]
        if not priced:
            continue
        pick = min(priced, key=lambda p: (abs(abs(p.delta) - target_delta), p.contract.strike))
        # A newly listed expiry can have no open interest yet (KO's December
        # 2024 series at 2024-03-01): prefer the next one out that trades.
        if is_liquid(pick.contract):
            return pick
        picks.append(pick)
    # Nothing liquid anywhere: return the shortest, so the illiquidity screen
    # reports it rather than the rule hiding it.
    return picks[0] if picks else None


def is_liquid(contract: Contract) -> bool:
    return contract.open_interest >= MIN_OPEN_INTEREST and contract.spread <= MAX_SPREAD
