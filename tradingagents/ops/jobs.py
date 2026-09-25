"""Bounded background jobs the operator may start from a phone, with approval.

An adjudication takes minutes per name, far longer than an MCP call should
block, so an action starts a detached process and returns at once with a job
id; ``job_status`` reports it. Guards, all enforced here rather than trusted to
the caller:

- at most ``MAX_NAMES`` names per job;
- never inside the night window (01:30-07:30), when the nightly chain owns the
  budget and the sweep logs;
- never while another adjudication, the nightly queue or the harvest is running
  -- two writers on one sweep log could decide a name twice.

Approval is Hermes' job: these tools are not annotated read-only, and the
operator profile marks this server ``trust: untrusted``, so Hermes asks before
each call.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from datetime import datetime, time
from pathlib import Path

MAX_NAMES = 5
NIGHT_START, NIGHT_END = time(1, 30), time(7, 30)
_BUSY = re.compile(r"tradingagents (screen-run|nightly-queue|harvest)\b")
_TA = str(Path.home() / ".local" / "bin" / "tradingagents")


def jobs_dir(config: dict) -> Path:
    return Path(config["results_dir"]) / "ops" / "jobs"


def _busy() -> str | None:
    """The first running adjudication or harvest, if any."""
    out = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if _BUSY.search(line) and "ps -axo" not in line:
            return line.strip()[:120]
    return None


def refusal(now: datetime | None = None, busy=_busy) -> str | None:
    now = now or datetime.now()
    if NIGHT_START <= now.time() < NIGHT_END:
        return (f"Refused: {now:%H:%M} is inside the night window "
                f"({NIGHT_START:%H:%M}-{NIGHT_END:%H:%M}) that the nightly chain owns.")
    running = busy()
    if running:
        return f"Refused: another run is in progress ({running})."
    return None


def start(config: dict, kind: str, args: list[str], now: datetime | None = None, busy=_busy,
          launcher=subprocess.Popen) -> str:
    """Start ``tradingagents <args>`` detached; return a message with the job id."""
    reason = refusal(now, busy)
    if reason:
        return reason
    d = jobs_dir(config)
    d.mkdir(parents=True, exist_ok=True)
    job_id = f"{(now or datetime.now()):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    log = d / f"{job_id}.log"
    wrapped = f'"{_TA}" {" ".join(_quote(a) for a in args)} < /dev/null > "{log}" 2>&1; echo "exit=$?" >> "{log}"'
    launcher(["/bin/bash", "-c", wrapped], start_new_session=True,
             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(Path.home()))
    (d / f"{job_id}.json").write_text(json.dumps({"id": job_id, "kind": kind, "args": args,
                                                  "started": (now or datetime.now()).isoformat(timespec="seconds")}))
    return f"Started job {job_id}: {kind}. Ask for job status to follow it."


def _quote(arg: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.,:=-]+", arg):
        raise ValueError(f"refusing an unexpected argument: {arg!r}")
    return arg


def status(config: dict, limit: int = 5) -> str:
    d = jobs_dir(config)
    metas = sorted(d.glob("*.json"), reverse=True)[:limit] if d.exists() else []
    if not metas:
        return "No operator jobs have been started."
    lines = []
    for meta in metas:
        m = json.loads(meta.read_text())
        log = meta.with_suffix(".log")
        text = log.read_text(errors="replace") if log.exists() else ""
        done = re.search(r"^exit=(\d+)$", text, re.M)
        state = f"finished, exit {done.group(1)}" if done else "running"
        ran = re.findall(r"^Ran (\d+) names?", text, re.M)
        failed = len(re.findall(r"^failed: ", text, re.M))
        lines.append(f"- {m['id']} {m['kind']}: {state}; names run {sum(map(int, ran))}, failed {failed} "
                     f"(started {m['started'][11:16]})")
    return "\n".join(lines)

