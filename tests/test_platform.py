"""The platform service: one schedule, the wakes it needs, and the chain runner."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tradingagents.ops import schedule

REPO = Path(__file__).resolve().parents[1]
NY = ZoneInfo("America/New_York")
darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="the wake daemon uses BSD date and pmset")


# --- the schedule ------------------------------------------------------------------------


@pytest.mark.unit
def test_every_waking_job_gets_a_wake_five_minutes_early():
    assert schedule.wake_times() == [time(1, 55), time(7, 55)]


@pytest.mark.unit
def test_next_run_rolls_over_midnight():
    job = schedule.Job("nightly", time(2, 0), "x")
    evening = datetime(2026, 9, 25, 22, 0, tzinfo=NY)
    assert schedule.next_run(job, evening) == datetime(2026, 9, 26, 2, 0, tzinfo=NY)
    early = datetime(2026, 9, 26, 1, 0, tzinfo=NY)
    assert schedule.next_run(job, early) == datetime(2026, 9, 26, 2, 0, tzinfo=NY)


@pytest.mark.unit
def test_export_writes_only_hhmm_lines(tmp_path):
    path = schedule.export(tmp_path / "wake-schedule")
    assert path.read_text() == "01:55\n07:55\n"


# --- the wake daemon ----------------------------------------------------------------------

FAKE_PMSET = """#!/bin/bash
if [ "$1" = "-g" ]; then printf '%s\\n' "$FAKE_SCHED"; exit 0; fi
echo "$*" >> "$FAKE_LOG"
"""


def _daemon(tmp_path, schedule_lines, sched_out, now):
    pmset = tmp_path / "pmset"
    pmset.write_text(FAKE_PMSET)
    pmset.chmod(0o755)
    wake = tmp_path / "wake-schedule"
    wake.write_text("".join(f"{line}\n" for line in schedule_lines))
    calls = tmp_path / "calls.log"
    env = {**os.environ, "PMSET": str(pmset), "WAKE_SCHEDULE": str(wake), "FAKE_LOG": str(calls),
           "FAKE_SCHED": sched_out, "NOW_EPOCH": str(int(now.timestamp())), "TZ": "America/New_York"}
    out = subprocess.run(["/bin/bash", str(REPO / "scripts" / "wake-daemon.sh")],
                         env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return (calls.read_text().splitlines() if calls.exists() else []), out.stdout


REPEAT_0155 = "Repeating power events:\n  wakepoweron at 1:55AM every day\nScheduled power events:\n"
EVENING = datetime(2026, 9, 25, 20, 0, tzinfo=NY)


@darwin_only
@pytest.mark.unit
def test_a_repeating_wake_covers_its_time_and_the_rest_are_scheduled(tmp_path):
    calls, _ = _daemon(tmp_path, ["01:55", "07:55"], REPEAT_0155, EVENING)
    assert calls == ["schedule wake 09/26/26 07:55:00 tradingagents"]


@darwin_only
@pytest.mark.unit
def test_running_again_adds_nothing(tmp_path):
    sched = REPEAT_0155 + " [0]  wake at 09/26/2026 07:55:00 by 'tradingagents'\n"
    calls, _ = _daemon(tmp_path, ["01:55", "07:55"], sched, EVENING)
    assert calls == []


@darwin_only
@pytest.mark.unit
def test_someone_elses_wake_at_that_time_is_enough(tmp_path):
    sched = REPEAT_0155 + " [0]  wake at 09/26/2026 07:55:00 by 'com.apple.alarm'\n"
    calls, _ = _daemon(tmp_path, ["07:55"], sched, EVENING)
    assert calls == []


@darwin_only
@pytest.mark.unit
def test_only_our_own_stale_wakes_are_cancelled(tmp_path):
    sched = (REPEAT_0155
             + " [0]  wake at 09/26/2026 06:00:00 by 'tradingagents'\n"
             + " [1]  wake at 09/26/2026 06:30:00 by 'com.apple.alarm'\n")
    calls, _ = _daemon(tmp_path, ["07:55"], sched, EVENING)
    assert "schedule cancel wake 09/26/26 06:00:00 tradingagents" in calls
    assert not any("06:30" in c for c in calls), "another owner's wake is never touched"
    assert not any("repeat" in c for c in calls), "the repeating wake is never changed"


@darwin_only
@pytest.mark.unit
def test_a_malformed_line_never_reaches_pmset(tmp_path):
    calls, out = _daemon(tmp_path, ["07:55", "25:00", "07:55; rm -rf /", "$(reboot)"], "", EVENING)
    assert calls == ["schedule wake 09/26/26 07:55:00 tradingagents"]
    assert out.count("ignoring malformed line") == 3


@darwin_only
@pytest.mark.unit
def test_a_time_already_passed_today_is_scheduled_for_tomorrow(tmp_path):
    morning = datetime(2026, 9, 25, 7, 0, tzinfo=NY)
    calls, _ = _daemon(tmp_path, ["06:00", "07:55"], "", morning)
    assert sorted(calls) == ["schedule wake 09/25/26 07:55:00 tradingagents",
                             "schedule wake 09/26/26 06:00:00 tradingagents"]


@darwin_only
@pytest.mark.unit
def test_the_wall_clock_holds_across_the_end_of_daylight_saving(tmp_path):
    """DST ends 2026-11-01 at 02:00: tomorrow's 07:55 is still 07:55 local."""
    before = datetime(2026, 10, 31, 20, 0, tzinfo=NY)
    calls, _ = _daemon(tmp_path, ["07:55"], "", before)
    assert calls == ["schedule wake 11/01/26 07:55:00 tradingagents"]


@darwin_only
@pytest.mark.unit
def test_no_schedule_file_is_a_quiet_no_op(tmp_path):
    pmset = tmp_path / "pmset"
    pmset.write_text(FAKE_PMSET)
    pmset.chmod(0o755)
    env = {**os.environ, "PMSET": str(pmset), "WAKE_SCHEDULE": str(tmp_path / "missing"),
           "FAKE_LOG": str(tmp_path / "calls.log"), "FAKE_SCHED": ""}
    out = subprocess.run(["/bin/bash", str(REPO / "scripts" / "wake-daemon.sh")],
                         env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and "nothing to do" in out.stdout
    assert not (tmp_path / "calls.log").exists()


# --- the chain runner -----------------------------------------------------------------------


def _chain(tmp_path, nightly_exit, harvest_exit=None):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    runner = repo / "scripts" / "platform-run.sh"
    runner.write_text((REPO / "scripts" / "platform-run.sh").read_text())
    runner.chmod(0o755)
    trace = tmp_path / "trace"
    for name, code in (("nightly", nightly_exit), ("harvest", harvest_exit)):
        if code is None:
            continue
        s = repo / "scripts" / f"{name}.sh"
        s.write_text(f"#!/bin/bash\necho {name} >> {trace}\nexit {code}\n")
        s.chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path), "TA_CAFFEINATED": "1"}
    out = subprocess.run([str(runner)], env=env, capture_output=True, text=True, timeout=30)
    log = next((tmp_path / ".tradingagents" / "logs" / "platform").glob("*.log")).read_text()
    return out.returncode, trace.read_text().split(), log


@pytest.mark.unit
def test_the_chain_runs_the_harvest_after_a_failed_nightly_and_returns_the_failure(tmp_path):
    code, ran, log = _chain(tmp_path, nightly_exit=2, harvest_exit=0)
    assert ran == ["nightly", "harvest"]
    assert code == 2
    assert "end nightly exit=2" in log and "end harvest exit=0" in log


@pytest.mark.unit
def test_the_chain_skips_a_harvest_that_is_not_installed_yet(tmp_path):
    code, ran, log = _chain(tmp_path, nightly_exit=0)
    assert ran == ["nightly"] and code == 0
    assert "skip harvest" in log


# --- the installer and its templates ------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("script", ["wake-daemon.sh", "platform-run.sh", "install-platform.sh", "nightly.sh"])
def test_the_shell_scripts_parse(script):
    out = subprocess.run(["/bin/bash", "-n", str(REPO / "scripts" / script)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


@darwin_only
@pytest.mark.unit
@pytest.mark.parametrize("plist", sorted((REPO / "scripts" / "launchd").glob("*.plist")), ids=lambda p: p.stem)
def test_the_launchd_templates_are_valid(plist):
    out = subprocess.run(["plutil", "-lint", str(plist)], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout


@pytest.mark.unit
def test_the_hermes_agent_runs_the_gateway_under_an_external_supervisor():
    text = (REPO / "scripts" / "launchd" / "com.jeremysmith.tradingagents.hermes.plist").read_text()
    assert "<string>run</string>" in text and "--external-supervisor" in text
    assert "<key>KeepAlive</key>" in text


@pytest.mark.unit
def test_the_nightly_agent_runs_the_chain_not_the_bare_nightly_script():
    text = (REPO / "scripts" / "launchd" / "com.jeremysmith.tradingagents.nightly.plist").read_text()
    assert "__REPO__/scripts/platform-run.sh" in text
