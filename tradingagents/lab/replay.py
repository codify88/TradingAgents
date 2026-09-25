"""Replay a screen on every schedule date and score it on the ladder.

On each date the screen is what the live one does with prices alone:

1. the universe is that month's listings (tier 0, delisted names included);
2. investability, from data on or before the date: an as-traded close of at
   least $5, a median 63-day dollar volume above the floor, a year of closes;
3. one ordering signal ranks the eligible names; the top ``picks`` are picks,
   every other eligible name is the pool (the controls, all of them, which is
   the same expectation as a random draw of three with far less noise).

Returns are traded the way the order plan would trade them: bought at the next
day's open, sold at the open ``horizon`` trading days later. A name that stops
trading inside the window is sold at its last close -- how a delisting ends a
holding -- and the date is skipped only when the window has not finished.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from statistics import NormalDist

import numpy as np
import pandas as pd

from .panel import Panel

MIN_PRICE = 5.0
HISTORY_BARS = 252
DV_WINDOW = 63
# Round-trip cost assumed per holding: 5 basis points a side.
COST_PER_SIDE = 0.0005


@contextmanager
def _quiet():
    """All-NaN columns (a name not yet listed) are expected, not news."""
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        yield


# --- strategies -------------------------------------------------------------------


@dataclass(frozen=True)
class Strategy:
    name: str
    horizon: int              # trading days held
    style: str                # the style index on the ladder
    every: str                # schedule: "week" or "month"
    signals: tuple[str, ...]
    picks: tuple[int, ...] = (8, 20)
    min_dollar_volume: tuple[float, ...] = (5e6, 25e6)
    # Rank only within the N most liquid eligible names (None: all of them).
    # The live value and momentum screens rank within their 60-name liquidity budget.
    pools: tuple[int | None, ...] = (None,)
    note: str = ""
    fundamentals: bool = False             # rank and exclude on point-in-time value facts
    qualities: tuple[bool, ...] = (True,)  # with / without the quality exclusions
    split: str = "2020-01-01"              # tune before, test from
    read_horizons: tuple[int, ...] = ()    # shorter holds reported beside the verdict


STRATEGIES = {
    "standard": Strategy(
        "standard", 5, "SPY", "week",
        ("liquidity", "random", "reversal_5d", "reversal_21d", "momentum_21d",
         "momentum_12_1", "low_vol_63", "high_52w"),
        note="The no-mandate strategy's candidate screen: price-only by design."),
    "momentum": Strategy(
        "equity_momentum", 126, "MTUM", "month",
        ("excess_12m", "momentum_12_1", "high_52w", "low_vol_63", "random", "liquidity"),
        pools=(None, 60),
        note="Price tier and ordering only: the live screen's trend and growth exclusions are not "
             "replayed yet. Pool 60 ranks within the 60 most liquid names, as the live screen does."),
    "value": Strategy(
        "equity_value", 756, "IWD", "month",
        ("fcf_pct", "fcf_yield", "ey_pct", "ebit_pct", "liquidity", "random"),
        min_dollar_volume=(5e6,), pools=(60, 120), fundamentals=True, qualities=(True, False),
        split="2018-01-01", read_horizons=(252,),
        note="The live value screen on point-in-time statements: the 60 (or 120) most liquid names, "
             "the quality exclusions (or none: /noq), one cheapness ordering. Statements are restated "
             "figures and start in 2006. A 3-year hold sampled monthly gives about one independent "
             "holding every three years, so the 1-year read is shown beside it."),
}


@dataclass(frozen=True)
class Variant:
    signal: str
    picks: int
    min_dollar_volume: float
    pool: int | None = None
    quality: bool = True

    @property
    def id(self) -> str:
        base = f"{self.signal}/p{self.picks}/dv{self.min_dollar_volume / 1e6:g}m"
        return base + (f"/top{self.pool}" if self.pool else "") + ("" if self.quality else "/noq")


def variants(strategy: Strategy) -> list[Variant]:
    return [Variant(s, p, dv, pool, q) for s in strategy.signals for p in strategy.picks
            for dv in strategy.min_dollar_volume for pool in strategy.pools for q in strategy.qualities]


# --- the context: arrays, schedule, universes ---------------------------------------


class Context:
    def __init__(self, panel: Panel, universes: dict[str, list[str]]):
        self.dates = panel.dates
        self.symbols = np.array(panel.symbols)
        self.col = {s: j for j, s in enumerate(panel.symbols)}
        self.O = panel.open.to_numpy(np.float64)
        self.C = panel.close.to_numpy(np.float64)
        self.RC = panel.raw_close.to_numpy(np.float64)
        self.DVd = self.RC * panel.volume.to_numpy(np.float64)
        self._listings = sorted(universes)
        from tradingagents.screener.universe import test_issue

        # Universes saved before test issues were excluded may still hold them.
        self._members = {d: np.array([self.col[s] for s in syms if s in self.col and not test_issue(s)],
                                     dtype=int)
                         for d, syms in universes.items()}
        self._cache: dict[tuple, np.ndarray] = {}
        self.facts: dict[str, dict] = {}  # date -> {symbol: lab.fundamentals.Facts}, for value replays

    def set_facts(self, facts: dict) -> None:
        """``{(symbol, date): Facts}`` as the fundamentals cache keeps them."""
        self.facts = {}
        for (symbol, day), f in facts.items():
            self.facts.setdefault(day, {})[symbol] = f

    def liquid_eligible(self, i: int, min_dollar_volume: float, pool: int | None) -> np.ndarray:
        """Eligible names on row ``i`` (listed, a year of closes, >= $5 as traded, above
        the liquidity floor, trading the next day), most liquid first, at most ``pool``."""
        cols = self.members(i)
        if cols.size == 0 or i + 1 >= len(self.dates):
            return cols[:0]
        ok = (self.history_ok(i)[cols] & (self.RC[i, cols] >= MIN_PRICE)
              & (self.dollar_volume(i)[cols] >= min_dollar_volume)
              & ~np.isnan(self.O[i + 1, cols]))
        cols = cols[ok]
        if pool and cols.size > pool:
            cols = cols[np.argsort(-self.dollar_volume(i)[cols], kind="stable")[:pool]]
        return cols

    def schedule(self, every: str, start: str, end: str | None = None) -> list[int]:
        """Row indices of the last trading day of each week or month."""
        idx = pd.Series(np.arange(len(self.dates)), index=self.dates)
        idx = idx[idx.index >= pd.Timestamp(start)]
        if end:
            idx = idx[idx.index < pd.Timestamp(end)]
        key = idx.index.to_period("W-FRI" if every == "week" else "M")
        return [int(v) for v in idx.groupby(key).max().to_numpy() if v >= HISTORY_BARS]

    def members(self, i: int) -> np.ndarray:
        day = self.dates[i].strftime("%Y-%m-%d")
        pos = np.searchsorted(self._listings, day, side="right") - 1
        return self._members[self._listings[pos]] if pos >= 0 else np.array([], dtype=int)

    def dollar_volume(self, i: int) -> np.ndarray:
        k = ("dv", i)
        if k not in self._cache:
            with _quiet():
                self._cache[k] = np.nanmedian(self.DVd[i - DV_WINDOW + 1:i + 1], axis=0)
        return self._cache[k]

    def history_ok(self, i: int) -> np.ndarray:
        k = ("hist", i)
        if k not in self._cache:
            n = np.sum(~np.isnan(self.C[i - HISTORY_BARS + 1:i + 1]), axis=0)
            self._cache[k] = n >= int(HISTORY_BARS * 0.95)
        return self._cache[k]

    def ret(self, i: int, k: int, lag: int = 0) -> np.ndarray:
        with np.errstate(all="ignore"):
            return self.C[i - lag] / self.C[i - k] - 1

    def forward(self, i: int, horizon: int, cols: np.ndarray) -> np.ndarray:
        """Next open to the open ``horizon`` days later; a name that stops trading
        exits at its last close in the window. NaN if it never traded after entry."""
        a, b = i + 1, i + 1 + horizon
        entry = self.O[a, cols]
        exit_ = self.O[b, cols].copy()
        gone = np.isnan(exit_)
        if gone.any():
            window = self.C[a:b + 1][:, cols[gone]]
            last = pd.DataFrame(window).ffill().iloc[-1].to_numpy()
            exit_[gone] = last
        with _quiet():
            return exit_ / entry - 1


def _judged(ctx: Context, i: int, cols: np.ndarray, quality: bool) -> np.ndarray:
    """Names the value screen could judge on row ``i``: facts computed and usable,
    and -- with ``quality`` -- no quality exclusion tripped. As live, a name that
    cannot be judged is neither a pick nor in the pool."""
    known = ctx.facts.get(ctx.dates[i].strftime("%Y-%m-%d"), {})
    keep = []
    for j in cols:
        f = known.get(str(ctx.symbols[j]))
        if f is None or f.error or (quality and f.tripped):
            continue
        keep.append(j)
    return np.array(keep, dtype=int)


def _fact_signal(ctx: Context, name: str, i: int) -> np.ndarray:
    out = np.full(len(ctx.symbols), np.nan)
    for symbol, f in ctx.facts.get(ctx.dates[i].strftime("%Y-%m-%d"), {}).items():
        if not f.error and symbol in ctx.col:
            out[ctx.col[symbol]] = f.values.get(name, np.nan)
    return out


def _signal(ctx: Context, name: str, i: int, seed: int) -> np.ndarray:
    from .fundamentals import MEASURES

    if name in MEASURES:
        return _fact_signal(ctx, name, i)
    with _quiet():
        if name == "liquidity":
            return ctx.dollar_volume(i)
        if name == "random":
            return np.random.default_rng(seed + i).random(len(ctx.symbols))
        if name == "reversal_5d":
            return -ctx.ret(i, 5)
        if name == "reversal_21d":
            return -ctx.ret(i, 21)
        if name == "momentum_21d":
            return ctx.ret(i, 21)
        if name == "momentum_12_1":
            return ctx.ret(i, 252, lag=21)
        if name == "excess_12m":
            spy = ctx.col.get("SPY")
            base = ctx.ret(i, 252)
            return base - (base[spy] if spy is not None else 0.0)
        if name == "low_vol_63":
            window = ctx.C[i - 63:i + 1]
            daily = window[1:] / window[:-1] - 1
            return -np.nanstd(daily, axis=0)
        if name == "high_52w":
            return ctx.C[i] / np.nanmax(ctx.C[i - 251:i + 1], axis=0)
    raise KeyError(f"unknown signal {name!r}")


SIGNALS = {
    "liquidity": "median 63-day dollar volume (no view: the most liquid names)",
    "random": "a random order (the null: selection should be ~0)",
    "reversal_5d": "last week's losers first (short-term reversal)",
    "reversal_21d": "last month's losers first",
    "momentum_21d": "last month's winners first",
    "momentum_12_1": "12-month return skipping the latest month",
    "excess_12m": "12-month return over SPY (the live momentum ordering)",
    "low_vol_63": "lowest 63-day volatility first",
    "high_52w": "closest to the 52-week high first",
    "fcf_pct": "FCF yield percentile in the company's own history (the live value ordering)",
    "fcf_yield": "trailing FCF yield, across companies",
    "ey_pct": "earnings yield percentile in the company's own history",
    "ebit_pct": "EBIT / EV percentile in the company's own history",
}


# --- one replay -------------------------------------------------------------------


@dataclass
class Row:
    date: str
    eligible: int
    picks: float
    pool: float
    market: float
    equal: float
    style: float
    chosen: list[str] = field(default_factory=list)

    @property
    def selection(self) -> float:
        return self.picks - self.pool


def replay(ctx: Context, strategy: Strategy, variant: Variant, dates: list[int],
           seed: int = 7, horizon: int | None = None) -> list[Row]:
    horizon = horizon or strategy.horizon
    bench = {s: ctx.col.get(s) for s in ("SPY", "RSP", strategy.style)}
    out = []
    for i in dates:
        if i + 1 + horizon >= len(ctx.dates):
            continue
        cols = ctx.liquid_eligible(i, variant.min_dollar_volume, variant.pool)
        if cols.size == 0:
            continue
        if strategy.fundamentals:
            cols = _judged(ctx, i, cols, variant.quality)
        score = _signal(ctx, variant.signal, i, seed)[cols] if cols.size else cols.astype(float)
        keep = ~np.isnan(score)
        cols, score = cols[keep], score[keep]
        if cols.size <= variant.picks:
            continue
        order = np.argsort(-score, kind="stable")
        chosen, pool = cols[order[:variant.picks]], cols[order[variant.picks:]]
        fwd_p, fwd_c = ctx.forward(i, horizon, chosen), ctx.forward(i, horizon, pool)

        def one(sym: str, i: int = i) -> float:
            j = bench.get(sym)
            return float(ctx.forward(i, horizon, np.array([j]))[0]) if j is not None else math.nan

        out.append(Row(
            date=ctx.dates[i].strftime("%Y-%m-%d"), eligible=int(cols.size),
            picks=_nanmean(fwd_p), pool=_nanmean(fwd_c),
            market=one("SPY"), equal=one("RSP"), style=one(strategy.style),
            chosen=[str(s) for s in ctx.symbols[chosen]]))
    return out


def _nanmean(x: np.ndarray) -> float:
    with _quiet():
        return float(np.nanmean(x))


# --- statistics -------------------------------------------------------------------


def newey_west_t(x: np.ndarray, lags: int) -> float:
    """Mean over its Newey-West standard error: overlapping holdings (a 126-day
    hold sampled monthly) make neighbouring dates share returns, and a plain
    t-stat would count each shared return several times."""
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 3:
        return math.nan
    d = x - x.mean()
    s = d @ d / n
    for k in range(1, min(lags, n - 1) + 1):
        s += 2 * (1 - k / (lags + 1)) * (d[k:] @ d[:-k]) / n
    return float(x.mean() / math.sqrt(s / n)) if s > 0 else math.nan


def lags_for(strategy: Strategy, horizon: int | None = None) -> int:
    step = 5 if strategy.every == "week" else 21
    return max(0, math.ceil((horizon or strategy.horizon) / step) - 1)


@dataclass
class Stats:
    n: int
    selection: float          # picks - pool, before costs
    t: float
    hit: float                # share of dates selection > 0
    net_vs_market: float      # picks after a round-trip cost, minus the S&P 500
    t_net: float
    style: float              # style - S&P 500
    universe: float           # pool - style
    size: float               # RSP - S&P 500
    independent: float = math.nan  # holdings that do not overlap: n / (lags + 1)


def stats(rows: list[Row], strategy: Strategy, horizon: int | None = None) -> Stats:
    if not rows:
        return Stats(0, *([math.nan] * 8))
    f = pd.DataFrame([asdict(r) for r in rows])
    sel = (f.picks - f.pool).to_numpy()
    net = (f.picks - 2 * COST_PER_SIDE - f.market).to_numpy()
    lags = lags_for(strategy, horizon)
    return Stats(
        n=len(f), selection=float(np.nanmean(sel)), t=newey_west_t(sel, lags),
        hit=float(np.mean(sel > 0)), net_vs_market=float(np.nanmean(net)), t_net=newey_west_t(net, lags),
        style=float(np.nanmean(f["style"] - f.market)), universe=float(np.nanmean(f.pool - f["style"])),
        size=float(np.nanmean(f.equal - f.market)), independent=len(f) / (lags + 1))


# --- the bar ----------------------------------------------------------------------

OOS_T = 2.0
FAMILY_ALPHA = 0.05


def t_hurdle(trials: int) -> float:
    """The in-sample t a variant needs when ``trials`` variants have been tried:
    a two-sided 5% test, Bonferroni-corrected for the whole search. Crude, and
    deliberately conservative -- the search is cheap, so it will be large."""
    return NormalDist().inv_cdf(1 - FAMILY_ALPHA / (2 * max(trials, 1)))


@dataclass
class Result:
    variant: Variant
    tune: Stats
    test: Stats
    recent: list[Row] = field(default_factory=list)
    reads: dict[int, tuple[Stats, Stats]] = field(default_factory=dict)  # horizon -> (tune, test)


def _split(rows: list[Row], cut: pd.Timestamp, horizon: int) -> tuple[list[Row], list[Row]]:
    # A tuning date whose holding ends after the split would let the test
    # period's prices into the tuning: those dates are left out of both.
    embargo = cut - pd.offsets.BDay(horizon + 1)
    return ([r for r in rows if pd.Timestamp(r.date) < embargo],
            [r for r in rows if pd.Timestamp(r.date) >= cut])


def evaluate(ctx: Context, strategy: Strategy, split: str | None, start: str,
             chosen: list[Variant] | None = None,
             progress: Callable[[str], None] | None = None) -> list[Result]:
    dates = ctx.schedule(strategy.every, start)
    cut = pd.Timestamp(split or strategy.split)
    out = []
    for v in chosen or variants(strategy):
        if progress:
            progress(v.id)
        rows = replay(ctx, strategy, v, dates)
        tune, test = _split(rows, cut, strategy.horizon)
        result = Result(v, stats(tune, strategy), stats(test, strategy), rows[-3:])
        for h in strategy.read_horizons:
            rt, rs = _split(replay(ctx, strategy, v, dates, horizon=h), cut, h)
            result.reads[h] = (stats(rt, strategy, h), stats(rs, strategy, h))
        out.append(result)
    return out
