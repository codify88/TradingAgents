"""Read a nightly log into facts, and say what about it needs attention.

The nightly job writes a plain-text log (``scripts/nightly.sh``). This turns it
into the things a person -- or the phone watcher -- wants to know: did it run,
when did the work actually start, how many cells ran and failed, the exit code,
the model usage, and whether the store saved requests. ``problems()`` lists what
is wrong in words, including the two failures that once went unnoticed: every
cell failing under exit code 0, and a 02:00 job that only worked at 07:00
because the Mac was asleep.

Shared by the MCP server's ``nightly_status`` tool and the missed-run check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_STAMP = re.compile(r"(\d{8}_\d{6})\.log$")

# How late the job itself may start after its schedule before it counts as late.
START_GRACE = timedelta(minutes=30)
# The screen step normally takes about 40 minutes (5,000+ names). Much longer
# means the Mac slept in the middle of it. Logs before step headers carried
# timestamps only show when the screen *finished*, so the same bound applies.
SCREEN_MAX = timedelta(minutes=90)


@dataclass
class NightlyRun:
    path: Path
    started: datetime                         # when the job itself started (the file's stamp)
    scheduled: datetime | None = None         # when it was meant to start
    mandate: str = ""
    max_names: int | None = None
    work_started: datetime | None = None      # when the screen finished (manifest time)
    steps: dict[str, datetime] = field(default_factory=dict)  # step -> start, newer logs
    finished: datetime | None = None
    exit_code: int | None = None
    screen_failed: bool = False
    screens: list[str] = field(default_factory=list)
    names_run: int = 0
    failures: list[tuple[str, str, str]] = field(default_factory=list)   # ticker, date, reason
    usage_lines: list[str] = field(default_factory=list)
    store_line: str = ""
    served_line: str = ""

    @property
    def start_late_by(self) -> timedelta | None:
        if self.scheduled is None:
            return None
        gap = self.started - self.scheduled
        return gap if gap > START_GRACE else None

    @property
    def screen_took(self) -> timedelta | None:
        """How long the screen step took: from step headers when present, else
        from the job start to the screen's manifest time."""
        if "screen" in self.steps and "adjudicate" in self.steps:
            return self.steps["adjudicate"] - self.steps["screen"]
        if self.work_started is not None:
            return self.work_started - self.started
        return None

    @property
    def slept(self) -> bool:
        took = self.screen_took
        return took is not None and took > SCREEN_MAX

    def problems(self) -> list[str]:
        out = []
        if self.finished is None:
            out.append("did not finish (still running, or stopped before the end)")
        if self.screen_failed:
            out.append("the screen failed, so no names were adjudicated")
        if self.exit_code not in (None, 0):
            out.append(f"exit code {self.exit_code}")
        if self.failures:
            out.append(f"{len(self.failures)} cell(s) failed")
            if self.names_run == 0:
                out.append("no cell succeeded")
        if self.start_late_by:
            out.append(f"meant to start at {self.scheduled:%H:%M} but started at {self.started:%H:%M}")
        if self.slept:
            hours = self.screen_took.total_seconds() / 3600
            out.append(f"the screen took {hours:.1f}h instead of about 40 minutes "
                       f"(the Mac was probably asleep during it)")
        return out

    def summary(self) -> str:
        lines = [f"Nightly run {self.started:%Y-%m-%d %H:%M} ({self.mandate or 'no mandate'}, "
                 f"max {self.max_names} names)"]
        if self.work_started:
            lines.append(f"- screen done {self.work_started:%H:%M}"
                         + (f", finished {self.finished:%H:%M}" if self.finished else ""))
        lines.append(f"- screens adjudicated: {', '.join(self.screens) or 'none'}")
        lines.append(f"- names run: {self.names_run}; failed cells: {len(self.failures)}")
        for ticker, day, reason in self.failures[:10]:
            lines.append(f"  - {ticker} {day}: {reason}")
        if len(self.failures) > 10:
            lines.append(f"  - ... and {len(self.failures) - 10} more")
        lines.extend(f"- {u}" for u in self.usage_lines)
        if self.store_line:
            lines.append(f"- {self.store_line}")
        if self.served_line:
            lines.append(f"- {self.served_line}")
        lines.append(f"- exit code: {self.exit_code if self.exit_code is not None else 'none recorded'}")
        problems = self.problems()
        lines.append("Needs attention: " + "; ".join(problems) if problems else "No problems found.")
        return "\n".join(lines)


def parse(path: str | Path, scheduled: str | None = "02:00") -> NightlyRun:
    """``scheduled`` is the job's HH:MM start, to tell a late start from a slow one;
    None for a run started by hand."""
    path = Path(path)
    m = _STAMP.search(path.name)
    if not m:
        raise ValueError(f"not a nightly log name: {path.name}")
    started = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
    run = NightlyRun(path=path, started=started)
    if scheduled:
        h, mi = (int(x) for x in scheduled.split(":"))
        run.scheduled = started.replace(hour=h, minute=mi, second=0)
        if run.scheduled > started + timedelta(hours=12):
            run.scheduled -= timedelta(days=1)
    failed_seen: set[tuple[str, str]] = set()
    in_store = False

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = _ANSI.sub("", raw).strip()
        if not line:
            continue
        if mm := re.match(r"--- step \d: (screen|adjudicate)\b.*\[(\d{2}):(\d{2}):(\d{2})\] ---", line):
            h, mi, s = (int(mm.group(i)) for i in (2, 3, 4))
            at = run.started.replace(hour=h, minute=mi, second=s)
            run.steps[mm.group(1)] = at + timedelta(days=1) if at < run.started else at
        elif mm := re.match(r"mandate=(\S+) max_names=(\d+)", line):
            run.mandate, run.max_names = mm.group(1), int(mm.group(2))
        elif mm := re.search(r"\(run (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\)", line):
            run.work_started = run.work_started or datetime.fromisoformat(mm.group(1))
        elif line.startswith("screen failed"):
            run.screen_failed = True
        elif mm := re.match(r"Screen (\S+) \(", line):
            run.screens.append(mm.group(1))
        elif mm := re.match(r"Ran (\d+) names?", line):
            run.names_run += int(mm.group(1))
        elif mm := re.match(r"failed: (\S+) (\d{4}-\d{2}-\d{2}): (.*?)(?: -- run again to retry)?$", line):
            key = (mm.group(1), mm.group(2))
            if key not in failed_seen:
                failed_seen.add(key)
                run.failures.append((mm.group(1), mm.group(2), mm.group(3)))
        elif line.startswith("Model usage:"):
            run.usage_lines.append(line)
        elif line.startswith("--- data store ---"):
            in_store = True
        elif in_store and line.startswith("Data store"):
            run.store_line = line
        elif in_store and "served without a fetch" in line:
            run.served_line = line
        elif mm := re.match(r"=== finished (\d{2}):(\d{2}):(\d{2}), screen-run exit=(\d+) ===", line):
            h, mi, s = (int(mm.group(i)) for i in (1, 2, 3))
            finished = run.started.replace(hour=h, minute=mi, second=s)
            if finished < run.started:
                finished += timedelta(days=1)
            run.finished, run.exit_code = finished, int(mm.group(4))
    return run


def log_for(log_dir: str | Path, day: date) -> Path | None:
    """The latest log started on ``day``, if the job ran that day."""
    matches = sorted(Path(log_dir).glob(f"{day:%Y%m%d}_*.log"))
    return matches[-1] if matches else None


def missed_today(latest_log: Path, now: datetime, scheduled: str = "02:00",
                 grace: timedelta = timedelta(hours=1)) -> str | None:
    """A sentence when today's run should have started by now and has not."""
    h, mi = (int(x) for x in scheduled.split(":"))
    due = now.replace(hour=h, minute=mi, second=0, microsecond=0) + grace
    m = _STAMP.search(latest_log.name)
    last = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S") if m else None
    if now >= due and (last is None or last.date() < now.date()):
        return (f"No nightly run has started today (due {scheduled}); the latest is from "
                f"{last:%Y-%m-%d %H:%M}." if last else f"No nightly run has started today (due {scheduled}).")
    return None


def latest(log_dir: str | Path) -> Path | None:
    logs = sorted(p for p in Path(log_dir).glob("*.log") if _STAMP.search(p.name))
    return logs[-1] if logs else None
