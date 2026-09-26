"""The market's state, day by day: the main trend, the shape of the recent path,
volatility and breadth -- each from a trailing window that ends at that day's
close, so a date's label uses nothing that came after it.

One day says nothing about whether the market is trending, ranging or breaking
out; the path over weeks does. Two window sets, one per holding horizon:

- ``long`` for value and momentum (held months to years): a 200-day trend, a
  60-day shape, breakouts from a 60-day range held 3 closes;
- ``short`` for the standard strategy (held a week): a 50-day trend, a 15-day
  shape, breakouts from a 20-day range held 2 closes.

The shape follows the market cycle traders describe (Al Brooks among them): a
breakout, then a channel, then a trading range, then the next breakout.

- **channel**: price moved efficiently one way -- over the shape window, the
  net move is a large share of all the daily moves added up (Kaufman's
  efficiency ratio);
- **range**: price stays in a band narrower than a random walk's with the same
  daily moves -- it keeps being pulled back;
- **breakout**: every one of the last ``hold`` closes is beyond the range the
  market kept before them, by at least half a day's typical move. It stays a
  breakout while price holds beyond that range, up to ``breakout_days``; if
  price falls back inside, it is a failed breakout and the path decides again.

A label changes only after ``confirm`` days in a row agree (a confirmed breakout
counts at once: its hold is its confirmation), so one odd day cannot flip it.
Closes only: the lab's panel has no intraday highs and lows.

These labels are descriptions, not trials: nothing here chooses a screen. A lab
variant that conditions on them is a trial like any other, and is counted.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .panel import lab_dir

TRENDS = ("up", "mixed", "down")
SHAPES = ("breakout_up", "channel_up", "range", "channel_down", "breakout_down", "unclear")


@dataclass(frozen=True)
class Profile:
    name: str
    trend_ma: int          # the main trend's moving average
    slope_lag: int         # the average is rising if above its value this many days ago
    shape: int             # the window for the path's shape; its recent half is read too
    channel_er: float      # efficiency at or above over the window: a channel
    leg_er: float          # at or above over the recent half alone: a new leg, a channel now
    range_band: float      # price band / (daily sigma x sqrt(window)) at or below: a range
    range_days: int        # the range a breakout leaves
    hold: int              # closes beyond it that confirm a breakout
    breakout_days: int     # longest a breakout label lasts before the path decides
    vol: int               # realised-volatility window
    stress_vol: float      # annualised volatility above this is stressed
    confirm: int           # days in a row a new label needs


PROFILES = {
    # Cut-offs from SPY's own distributions, 2011-2026: a channel is about the top
    # third of efficiency over the window, a new leg the top fifth over its recent
    # half, a range the narrowest 40% of bands (a random walk's band averages 1.6).
    "long": Profile("long", 200, 20, 60, 0.18, 0.40, 1.30, 60, 3, 20, 63, 0.20, 3),
    "short": Profile("short", 50, 10, 15, 0.35, 0.65, 1.15, 20, 2, 7, 20, 0.20, 2),
}
FOR_STRATEGY = {"standard": "short", "momentum": "long", "value": "long"}
BENCHMARK = "SPY"


def efficiency_ratio(close: pd.Series, n: int) -> pd.Series:
    """Net move over n days divided by the sum of the daily moves: 1 is a
    straight line, near 0 is going nowhere."""
    path = close.diff().abs().rolling(n).sum()
    return (close - close.shift(n)).abs() / path.replace(0, np.nan)


def trend(close: pd.Series, p: Profile) -> pd.Series:
    ma = close.rolling(p.trend_ma).mean()
    rising = ma > ma.shift(p.slope_lag)
    falling = ma < ma.shift(p.slope_lag)
    out = pd.Series("mixed", index=close.index, dtype=object)
    out[(close > ma) & rising] = "up"
    out[(close < ma) & falling] = "down"
    out[ma.isna()] = None
    return out


def _raw_shapes(close: pd.Series, p: Profile) -> pd.DataFrame:
    """Each day's shape before confirmation, and whether a breakout was confirmed that day."""
    rets = close.pct_change()
    sigma = rets.rolling(p.vol).std()
    margin = 0.5 * sigma * close
    # The range the market kept before the last `hold` closes.
    hi = close.shift(p.hold).rolling(p.range_days).max()
    lo = close.shift(p.hold).rolling(p.range_days).min()
    beyond_up = pd.concat([close.shift(k) > hi + margin for k in range(p.hold)], axis=1).all(axis=1)
    beyond_dn = pd.concat([close.shift(k) < lo - margin for k in range(p.hold)], axis=1).all(axis=1)
    half = max(2, p.shape // 2)
    er, er_half = efficiency_ratio(close, p.shape), efficiency_ratio(close, half)
    net, net_half = close - close.shift(p.shape), close - close.shift(half)

    # A range is contained: its band is narrower than a random walk's with the same
    # daily moves, so price keeps coming back. A rise then a fall of the same size
    # nets to nothing, but its band is wide, so it is not a range.
    logc = np.log(close)
    band = (logc.rolling(p.shape).max() - logc.rolling(p.shape).min()) / (
        logc.diff().rolling(p.shape).std() * np.sqrt(p.shape))
    shape = pd.Series("unclear", index=close.index, dtype=object)
    shape[band <= p.range_band] = "range"
    same_way = np.sign(net) == np.sign(net_half)
    shape[(er >= p.channel_er) & same_way & (net > 0)] = "channel_up"
    shape[(er >= p.channel_er) & same_way & (net < 0)] = "channel_down"
    # A strong recent leg is a channel now, whatever the older half did.
    shape[(er_half >= p.leg_er) & (net_half > 0)] = "channel_up"
    shape[(er_half >= p.leg_er) & (net_half < 0)] = "channel_down"
    shape[er.isna()] = None

    # A breakout lasts while price holds beyond the range it left, up to breakout_days.
    labels = shape.to_numpy(dtype=object).copy()
    confirmed = np.zeros(len(close), dtype=bool)
    level, side, age = np.nan, 0, 0
    c = close.to_numpy()
    up, dn, his, los = beyond_up.to_numpy(), beyond_dn.to_numpy(), hi.to_numpy(), lo.to_numpy()
    for i in range(len(c)):
        if side and age < p.breakout_days and (c[i] > level if side > 0 else c[i] < level):
            labels[i] = "breakout_up" if side > 0 else "breakout_down"
            age += 1
            continue
        side = 0
        # A breakout leaves a range: new highs inside an up channel are the channel going on.
        before = labels[i - p.hold] if i >= p.hold else None
        if up[i] and before not in ("channel_up", "breakout_up"):
            side, level, age = 1, his[i], 1
        elif dn[i] and before not in ("channel_down", "breakout_down"):
            side, level, age = -1, los[i], 1
        if side:
            labels[i] = "breakout_up" if side > 0 else "breakout_down"
            confirmed[i] = True
    return pd.DataFrame({"raw": labels, "confirmed": confirmed, "er": er, "band": band},
                        index=close.index)


def _hysteresis(raw: pd.Series, confirmed: pd.Series, n: int) -> tuple[list, list]:
    """A new label takes over only after n days in a row of it (a confirmed
    breakout at once); return the labels and the days each has lasted."""
    out, days = [], []
    cur, run_label, run = None, None, 0
    since = 0
    for label, conf in zip(raw, confirmed, strict=True):
        if not isinstance(label, str):  # the window is not full yet
            out.append(None)
            days.append(0)
            continue
        run = run + 1 if label == run_label else 1
        run_label = label
        # A new label takes over when it has lasted n days, at once when it is a
        # confirmed breakout, and at once when a breakout ends (held or failed).
        if label != cur and (cur is None or conf or run >= n or cur.startswith("breakout")):
            cur, since = label, 0
        since += 1
        out.append(cur)
        days.append(since)
    return out, days


def breadth(panel, trend_ma: int, min_dollar_volume: float = 5e6) -> pd.Series:
    """Share of liquid names above their own trend average, each day."""
    close = panel.close
    above = close > close.rolling(trend_ma, min_periods=trend_ma).mean()
    known = close.rolling(trend_ma, min_periods=trend_ma).mean().notna()
    liquid = (panel.raw_close * panel.volume).rolling(20, min_periods=10).median() > min_dollar_volume
    base = liquid & known
    n = base.sum(axis=1)
    return ((above & base).sum(axis=1) / n.replace(0, np.nan)).astype(float)


def compute(panel, profile: str | Profile, symbol: str = BENCHMARK) -> pd.DataFrame:
    p = PROFILES[profile] if isinstance(profile, str) else profile
    close = panel.close[symbol].dropna().astype(float)
    raw = _raw_shapes(close, p)
    shape, days = _hysteresis(raw["raw"], raw["confirmed"], p.confirm)
    vol = close.pct_change().rolling(p.vol).std() * np.sqrt(252)
    drawdown = close / close.rolling(252, min_periods=1).max() - 1
    t = trend(close, p)
    out = pd.DataFrame({
        "close": close, "trend": t, "shape": shape, "days": days, "er": raw["er"], "band": raw["band"], "vol": vol,
        "drawdown": drawdown, "breadth": breadth(panel, p.trend_ma).reindex(close.index),
    })
    out["stressed"] = (out["trend"] == "down") | (out["vol"] > p.stress_vol)
    return out


def episodes(states: pd.DataFrame, column: str = "shape") -> pd.DataFrame:
    """Runs of one label: start, end, days, and the benchmark's move over the run."""
    s = states[column]
    run = (s != s.shift()).cumsum()
    g = states.assign(run=run).dropna(subset=[column]).groupby("run")
    return pd.DataFrame({
        "label": g[column].first(), "start": g.apply(lambda f: f.index[0], include_groups=False),
        "end": g.apply(lambda f: f.index[-1], include_groups=False), "days": g.size(),
        "move": g["close"].last() / g["close"].first() - 1,
    }).reset_index(drop=True)


def summary(states: pd.DataFrame) -> dict:
    last = states.dropna(subset=["shape", "trend"]).iloc[-1]
    return {"as_of": str(last.name.date()), "trend": last["trend"], "shape": last["shape"],
            "days": int(last["days"]), "er": _f(last["er"]), "band": _f(last["band"]),
            "vol": _f(last["vol"]),
            "drawdown": _f(last["drawdown"]), "breadth": _f(last["breadth"]),
            "stressed": bool(last["stressed"])}


def _f(x) -> float | None:
    return None if x is None or pd.isna(x) else round(float(x), 4)


def save(config: dict, profile: str, states: pd.DataFrame) -> None:
    """The series (for the lab and the chart) and its latest state (for Desk)."""
    d = lab_dir(config)
    states.to_pickle(d / f"regime-{profile}.pkl")
    rows = states.dropna(subset=["shape", "trend"])
    # Column access by name: a row's .shape is its dimensions, not the column.
    history = [{"date": str(i.date()), "close": _f(r["close"]), "trend": r["trend"], "shape": r["shape"],
                "stressed": bool(r["stressed"]), "breadth": _f(r["breadth"])} for i, r in rows.iterrows()]
    (d / f"regime-{profile}.json").write_text(json.dumps(
        {"profile": asdict(PROFILES[profile]), "current": summary(states), "history": history}))


def load(config: dict, profile: str) -> dict | None:
    try:
        return json.loads((lab_dir(config) / f"regime-{profile}.json").read_text())
    except (OSError, ValueError):
        return None


def load_series(config: dict, profile: str) -> pd.DataFrame | None:
    path = lab_dir(config) / f"regime-{profile}.pkl"
    return pd.read_pickle(path) if path.exists() else None
