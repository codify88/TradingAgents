"""The harvest: pull more than today's agents use, ahead of need.

One run works through the due work in a fixed priority order until its request
budget or its deadline, whichever comes first, and a later run continues from
there -- "due" is decided from what the store already holds, so nothing is
tracked separately and an interrupted run loses nothing.

Order (measured and agreed 2026-09-25; docs/design/implementation-plan.md):

1. ``POLITICIAN_METADATA`` weekly, and institutional holdings snapshots weekly --
   holdings keep no history on the vendor side, so they go first;
2. ``EARNINGS`` weekly and ``OVERVIEW`` quarterly per name: they date and label
   every transcript;
3. ETF profiles: each once, then the largest ``WEEKLY_ETFS`` weekly, the rest
   monthly;
4. insider and congressional trades: priority names daily, the rest weekly;
5. every due transcript quarter of the priority names (saved screens and
   decisions);
6. every other name's transcripts, newest quarter first across all names.

It never fetches a price history to decide the universe: the price-tier
survivors are read from histories the nightly screen already stored.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tradingagents.datastore import get_store, graph, has_fresh
from tradingagents.datastore.policy import NEW_YORK
from tradingagents.datastore.store import params_key

from . import datasets as ds

logger = logging.getLogger(__name__)

# Stop after this many vendor failures in a row: that is an outage, not a name.
MAX_CONSECUTIVE_FAILURES = 25
RATE_LIMIT_PAUSE_SECONDS = 65
# A universe computed from fewer stored histories than this is not trusted
# (a fresh store, or a night the screen did not run); the last good one is used.
MIN_UNIVERSE = 500
UNIVERSE_MAX_AGE_DAYS = 10
TRANSCRIPTS_FROM = "2010-01-01"


@dataclass(frozen=True)
class Task:
    stage: str
    endpoint: str
    params: dict
    symbol: str | None = None


@dataclass
class HarvestReport:
    started: datetime
    requests: int = 0
    served: int = 0
    by_stage: Counter = field(default_factory=Counter)
    failures: list[tuple[str, str, str]] = field(default_factory=list)
    stopped: str = ""
    finished: datetime | None = None
    names: int = 0
    etfs: int = 0

    def render(self) -> str:
        took = (self.finished - self.started).total_seconds() / 60 if self.finished else 0
        lines = [f"Harvest {self.started:%Y-%m-%d %H:%M}: {self.requests:,} requests in {took:.0f} min "
                 f"({self.served:,} already fresh in the store); stopped: {self.stopped}.",
                 f"Universe: {self.names:,} names, {self.etfs:,} ETFs."]
        for stage, n in self.by_stage.most_common():
            lines.append(f"- {stage}: {n:,}")
        if self.failures:
            lines.append(f"- failed: {len(self.failures)} (e.g. {'; '.join(f'{e} {s}' for e, s, _ in self.failures[:3])})")
        return "\n".join(lines)

    def asdict(self) -> dict:
        return {"started": self.started.isoformat(), "finished": self.finished.isoformat() if self.finished else None,
                "requests": self.requests, "served": self.served, "by_stage": dict(self.by_stage),
                "failures": len(self.failures), "stopped": self.stopped,
                "names": self.names, "etfs": self.etfs}


def _config() -> dict:
    from tradingagents.dataflows.config import get_config

    return get_config()


def harvest_dir(config: dict | None = None) -> Path:
    return Path((config or _config())["results_dir"]) / "harvest"


# -- the universe ---------------------------------------------------------------


def universe(store, today: str, config: dict | None = None) -> tuple[list[str], list[str]]:
    """Price-tier survivors (from stored histories) and active ETFs, cached per day."""
    from tradingagents.dataflows.alpha_vantage_common import _make_api_request
    from tradingagents.mandates.tools.financials import _daily_params, parse_daily_body
    from tradingagents.screener.screen import investability_exclusions
    from tradingagents.screener.universe import _rows, load_universe

    cache = harvest_dir(config) / "universe.json"
    cached = json.loads(cache.read_text()) if cache.exists() else None
    if cached and cached.get("day") == today:
        return cached["names"], cached["etfs"]

    now = datetime.now(UTC)
    names = []
    for c in load_universe(today):
        row = store.stored(ds.VENDOR, "TIME_SERIES_DAILY_ADJUSTED", params_key(_daily_params(c.symbol)))
        if row is None or (now - row[1]).days > UNIVERSE_MAX_AGE_DAYS:
            continue
        frame = parse_daily_body(row[0])
        if not frame.empty and not investability_exclusions(frame):
            names.append(c.symbol)
    etfs = sorted(r["symbol"] for r in _rows(_make_api_request("LISTING_STATUS", {}))
                  if r.get("assetType") == "ETF" and r.get("status") == "Active" and r.get("symbol"))

    if len(names) < MIN_UNIVERSE and cached:
        logger.warning("only %d stored histories pass the price tier; keeping the %s universe",
                       len(names), cached.get("day"))
        names = cached["names"]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"day": today, "names": sorted(names), "etfs": etfs}))
    return sorted(names), etfs


def priority_names(config: dict | None = None) -> list[str]:
    """Names in saved screens (picks and controls) and in any decision log."""
    from tradingagents.agents.utils.memory import TradingMemoryLog

    config = config or _config()
    results = Path(config["results_dir"])
    names: set[str] = set()
    for manifest in (results / "screens").glob("*.json"):
        try:
            m = json.loads(manifest.read_text())
        except ValueError:
            continue
        names.update(p["symbol"] for p in m.get("picks", []) + m.get("controls", []) if p.get("symbol"))
    logs = [*results.glob("backtest/*/trading_memory.md"), Path(config.get("memory_log_path") or "")]
    for log in logs:
        if log.name and log.exists():
            names.update(e["ticker"] for e in TradingMemoryLog({"memory_log_path": str(log)}).load_entries())
    return sorted(names)


# -- what is due ------------------------------------------------------------------


def _due(store, endpoint: str, params: dict, cadence: timedelta, now: datetime) -> bool:
    row = store.stored(ds.VENDOR, endpoint, params_key(params))
    return row is None or now - row[1] >= cadence


def _snapshot_due(store, endpoint: str, params: dict, today: str, days: int) -> bool:
    last = store.last_snapshot_day(ds.VENDOR, endpoint, params_key(params))
    return last is None or (datetime.fromisoformat(today) - datetime.fromisoformat(last)).days >= days


def transcript_quarters(store, symbol: str, today: str) -> list[tuple[str, str]]:
    """(fiscal quarter label, call date) for every reported quarter since 2010,
    newest first, from the stored EARNINGS and OVERVIEW. Empty until EARNINGS is stored."""
    earnings = store.stored(ds.VENDOR, "EARNINGS", params_key({"symbol": symbol}))
    if earnings is None:
        return []
    try:
        quarters = json.loads(earnings[0]).get("quarterlyEarnings") or []
    except ValueError:
        return []
    overview = store.stored(ds.VENDOR, "OVERVIEW", params_key({"symbol": symbol}))
    fye = 12
    if overview is not None:
        with contextlib.suppress(ValueError):
            fye = ds.fiscal_year_end_month(json.loads(overview[0]).get("FiscalYearEnd"))
    out = {}
    for q in quarters:
        end, reported = q.get("fiscalDateEnding") or "", q.get("reportedDate") or ""
        if end >= TRANSCRIPTS_FROM and reported and reported < today:
            out.setdefault(ds.fiscal_quarter(end, fye), reported)
    return sorted(out.items(), key=lambda kv: kv[1], reverse=True)


def _transcript_due(store, symbol: str, label: str, now: datetime) -> bool:
    row = store.get(ds.VENDOR, "EARNINGS_CALL_TRANSCRIPT", params_key({"symbol": symbol, "quarter": label}))
    if row is None:
        return True
    body, fetched, final = row
    return not final and now - fetched >= ds.TRANSCRIPT_RETRY


def _etf_rank(store, etfs: list[str]) -> set[str]:
    """The largest ETFs by net assets, from the last profile snapshot of each."""
    sized = []
    for e in etfs:
        node = store.node(f"etf:{e}")
        try:
            sized.append((float(node.get("net_assets") or 0), e) if node else (0.0, e))
        except (TypeError, ValueError):
            sized.append((0.0, e))
    sized.sort(reverse=True)
    return {e for size, e in sized[: ds.WEEKLY_ETFS] if size > 0}


def tasks(store, names: list[str], etfs: list[str], priority: list[str], now: datetime) -> Iterator[Task]:
    today = now.astimezone(NEW_YORK).date().isoformat()
    prio = list(dict.fromkeys(priority))
    prio_set = set(prio)
    everyone = prio + [n for n in names if n not in prio_set]

    if _due(store, "POLITICIAN_METADATA", {}, ds.WEEKLY, now):
        yield Task("politicians", "POLITICIAN_METADATA", {})
    for s in everyone:
        if _snapshot_due(store, "INSTITUTIONAL_HOLDINGS", {"symbol": s}, today, 7):
            yield Task("holdings", "INSTITUTIONAL_HOLDINGS", {"symbol": s}, s)
    for s in everyone:
        if _due(store, "EARNINGS", {"symbol": s}, ds.WEEKLY, now):
            yield Task("earnings", "EARNINGS", {"symbol": s}, s)
        if _due(store, "OVERVIEW", {"symbol": s}, ds.QUARTERLY, now):
            yield Task("overview", "OVERVIEW", {"symbol": s}, s)
    weekly = _etf_rank(store, etfs)
    for e in etfs:
        if _snapshot_due(store, "ETF_PROFILE", {"symbol": e}, today, 7 if e in weekly else 30):
            yield Task("etf profiles", "ETF_PROFILE", {"symbol": e}, e)
    for s in everyone:
        cadence = timedelta(hours=20) if s in prio_set else ds.WEEKLY
        for endpoint, stage in (("INSIDER_TRANSACTIONS", "insider"), ("CONGRESS_TRADES", "congress")):
            if _due(store, endpoint, {"symbol": s}, cadence, now):
                yield Task(stage, endpoint, {"symbol": s}, s)
    for s in prio:
        for label, _ in transcript_quarters(store, s, today):
            if _transcript_due(store, s, label, now):
                yield Task("transcripts (priority)", "EARNINGS_CALL_TRANSCRIPT", {"symbol": s, "quarter": label}, s)
    rest = {s: transcript_quarters(store, s, today) for s in names if s not in prio_set}
    depth = max((len(q) for q in rest.values()), default=0)
    for i in range(depth):
        for s, quarters in rest.items():
            if i < len(quarters) and _transcript_due(store, s, quarters[i][0], now):
                yield Task("transcripts", "EARNINGS_CALL_TRANSCRIPT", {"symbol": s, "quarter": quarters[i][0]}, s)


# -- running it -------------------------------------------------------------------

_FILL = {
    "INSIDER_TRANSACTIONS": lambda store, t, body, day: graph.fill_insider(store, t.symbol, body),
    "CONGRESS_TRADES": lambda store, t, body, day: graph.fill_congress(store, t.symbol, body),
    "INSTITUTIONAL_HOLDINGS": lambda store, t, body, day: graph.fill_institutional(store, t.symbol, body),
    "ETF_PROFILE": lambda store, t, body, day: graph.fill_etf(store, t.symbol, body, day),
    "OVERVIEW": lambda store, t, body, day: graph.fill_overview(store, t.symbol, body, day),
    "POLITICIAN_METADATA": lambda store, t, body, day: graph.fill_politicians(store, body),
}


def _stop_time(stop_at: str | None, started: datetime) -> datetime | None:
    """When a run started at ``started`` must stop, for a morning ``stop_at``.

    Before the stop time: today at that time. After it but before noon (the Mac
    woke late): now -- a late run must not spend the day competing for the
    request budget. From noon on (a run started by hand in the evening): the
    stop time tomorrow morning."""
    if not stop_at:
        return None
    h, m = (int(x) for x in stop_at.split(":"))
    stop = started.replace(hour=h, minute=m, second=0, microsecond=0)
    if started < stop:
        return stop
    return started if started.hour < 12 else stop + timedelta(days=1)


STAGES = ("politicians", "holdings", "earnings", "overview", "etf profiles", "insider", "congress",
          "transcripts (priority)", "transcripts")


def run(max_requests: int, stop_at: str | None = None, dry_run: bool = False,
        config: dict | None = None, sleep=time.sleep,
        only: set[str] | None = None, skip: set[str] | None = None) -> HarvestReport:
    """``only``/``skip`` restrict the stages (names in ``STAGES``); for validation
    and daytime runs -- the nightly job runs everything in order."""
    from tradingagents.dataflows.alpha_vantage_common import (
        AlphaVantageRateLimitError,
        _make_api_request,
    )

    config = config or _config()
    store = get_store(config)
    started = datetime.now().astimezone()
    report = HarvestReport(started=started)
    deadline = _stop_time(stop_at, started)
    today = datetime.now(UTC).astimezone(NEW_YORK).date().isoformat()
    names, etfs = universe(store, today, config)
    report.names, report.etfs = len(names), len(etfs)
    consecutive = 0

    for task in tasks(store, names, etfs, priority_names(config), datetime.now(UTC)):
        if (only and task.stage not in only) or (skip and task.stage in skip):
            continue
        if report.requests >= max_requests:
            report.stopped = f"budget ({max_requests:,} requests)"
            break
        if deadline and datetime.now().astimezone() >= deadline:
            report.stopped = f"deadline ({stop_at})"
            break
        if dry_run:
            report.requests += 1
            report.by_stage[task.stage] += 1
            continue
        fresh = has_fresh(ds.VENDOR, task.endpoint, task.params)
        try:
            body = _make_api_request(task.endpoint, task.params)
        except AlphaVantageRateLimitError:
            sleep(RATE_LIMIT_PAUSE_SECONDS)
            try:
                body = _make_api_request(task.endpoint, task.params)
            except AlphaVantageRateLimitError:
                report.stopped = "rate limit (still limited after a pause)"
                break
        except Exception as exc:  # one name's failure is not the run's
            report.failures.append((task.endpoint, task.symbol or "", str(exc)[:200]))
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                report.stopped = f"{consecutive} vendor failures in a row"
                break
            continue
        consecutive = 0
        if fresh:
            report.served += 1
        else:
            report.requests += 1
            report.by_stage[task.stage] += 1
        fill = _FILL.get(task.endpoint)
        if fill and isinstance(body, str) and body.lstrip().startswith("{"):
            try:
                fill(store, task, body, today)
            except Exception as exc:  # a malformed payload must not stop the harvest
                logger.warning("graph fill failed for %s %s: %s", task.endpoint, task.symbol, exc)
    else:
        report.stopped = "everything due is done"

    report.finished = datetime.now().astimezone()
    if not dry_run:
        runs = harvest_dir(config) / "runs.jsonl"
        runs.parent.mkdir(parents=True, exist_ok=True)
        with runs.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(report.asdict()) + "\n")
    return report


# -- how far along it is ------------------------------------------------------------


def status(config: dict | None = None) -> str:
    """Coverage of each dataset, the backfill's progress, and the last run."""
    config = config or _config()
    cache = harvest_dir(config) / "universe.json"
    if not cache.exists():
        return "The harvest has not run yet."
    store = get_store(config)
    u = json.loads(cache.read_text())
    names, etfs, today = u["names"], u["etfs"], u["day"]
    conn = store._conn()

    def count(endpoint: str, extra: str = "") -> int:
        return conn.execute(f"SELECT COUNT(DISTINCT symbol) FROM response WHERE vendor=? AND endpoint=? {extra}",
                            (ds.VENDOR, endpoint)).fetchone()[0]

    week_ago = (datetime.fromisoformat(today) - timedelta(days=7)).date().isoformat()
    held = conn.execute("SELECT COUNT(DISTINCT symbol) FROM snapshot WHERE endpoint='INSTITUTIONAL_HOLDINGS' "
                        "AND fetched_on > ?", (week_ago,)).fetchone()[0]
    etf_done = conn.execute("SELECT COUNT(DISTINCT symbol) FROM snapshot WHERE endpoint='ETF_PROFILE'").fetchone()[0]
    got = conn.execute("SELECT COUNT(*) FROM response WHERE endpoint='EARNINGS_CALL_TRANSCRIPT'").fetchone()[0]
    expected = sum(len(transcript_quarters(store, s, today)) for s in names)
    with_earnings = count("EARNINGS")
    lines = [f"Harvest coverage ({len(names):,} names, {len(etfs):,} ETFs, universe of {today}):",
             f"- institutional holdings snapshotted this week: {held:,}/{len(names):,}",
             f"- earnings calendars: {with_earnings:,}/{len(names):,}; insider: {count('INSIDER_TRANSACTIONS'):,}; "
             f"congress: {count('CONGRESS_TRADES'):,}",
             f"- ETF profiles: {etf_done:,}/{len(etfs):,}",
             f"- transcript quarters asked: {got:,} of {expected:,} known so far"
             + (f" ({got / expected:.0%})" if expected else "")
             + ("" if with_earnings >= len(names) else " (more become known as earnings calendars arrive)")]
    runs = harvest_dir(config) / "runs.jsonl"
    if runs.exists():
        last = json.loads(runs.read_text().splitlines()[-1])
        lines.append(f"- last run {last['started'][:16]}: {last['requests']:,} requests, stopped: {last['stopped']}")
    return "\n".join(lines)
