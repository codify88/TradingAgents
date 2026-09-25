"""Every timed job in one place, and the wakes they need.

launchd runs jobs but cannot wake a sleeping Mac; ``pmset`` can, and
``pmset repeat`` holds only one daily wake. So the schedule lives here, and
``tradingagents schedule export`` writes the wake times to a plain file that
the root wake daemon (``scripts/wake-daemon.sh``) reads. The daemon never runs
code from this repo: the file is a list of ``HH:MM`` lines, and the worst a
tampered one can do is wake the Mac.

Times are local wall-clock times, as launchd and pmset use them.

Design: docs/design/implementation-plan.md, stream S.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

# Wake this long before a job, so the system is up when launchd fires it.
WAKE_LEAD = timedelta(minutes=5)


@dataclass(frozen=True)
class Job:
    name: str
    at: time
    description: str
    wake: bool = True


JOBS: tuple[Job, ...] = (
    Job("nightly", time(2, 0), "screen, adjudicate, then harvest (scripts/platform-run.sh)"),
    Job("morning-watchers", time(8, 0), "Hermes: nightly health, new decisions"),
)


def next_run(job: Job, now: datetime) -> datetime:
    """The job's next start at or after ``now``, in local wall-clock time."""
    candidate = datetime.combine(now.date(), job.at, tzinfo=now.tzinfo)
    return candidate if candidate >= now else datetime.combine(
        now.date() + timedelta(days=1), job.at, tzinfo=now.tzinfo)


def wake_times(jobs: tuple[Job, ...] = JOBS) -> list[time]:
    """Daily wake times, one per waking job, earliest first."""
    out = set()
    for job in jobs:
        if job.wake:
            at = datetime.combine(datetime(2000, 1, 1), job.at) - WAKE_LEAD
            out.add(at.time())
    return sorted(out)


def export(path: str | Path, jobs: tuple[Job, ...] = JOBS) -> Path:
    """Write the wake schedule the root daemon reads: one ``HH:MM`` per line."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{t:%H:%M}\n" for t in wake_times(jobs))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body, encoding="ascii")
    tmp.replace(path)
    return path


def describe(now: datetime, jobs: tuple[Job, ...] = JOBS) -> str:
    lines = []
    for job in sorted(jobs, key=lambda j: j.at):
        wake = f", wakes at {(datetime.combine(now.date(), job.at) - WAKE_LEAD):%H:%M}" if job.wake else ""
        lines.append(f"{job.at:%H:%M} {job.name}: {job.description}{wake}; "
                     f"next {next_run(job, now):%a %Y-%m-%d %H:%M}")
    return "\n".join(lines)
