"""Reading the nightly log: the failures that once went unnoticed are named."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from tradingagents.ops.nightly_log import latest, log_for, parse

FIX = Path(__file__).parent / "fixtures" / "nightly"


@pytest.mark.unit
def test_the_night_nine_cells_failed_under_exit_zero():
    run = parse(FIX / "20260923_020005.log")
    assert run.exit_code == 0 and run.names_run == 3
    assert len(run.failures) == 9, "duplicate report lines are counted once"
    assert ("PYPL", "2019-09-03", "No OHLCV data available for PYPL.") in run.failures
    problems = run.problems()
    assert "9 cell(s) failed" in problems
    assert any("asleep" in p for p in problems), "02:00 start, screen done at 07:02"


@pytest.mark.unit
def test_a_normal_forty_minute_screen_is_not_late():
    run = parse(FIX / "20260924_020003.log")
    assert run.problems() == []
    assert run.finished == datetime(2026, 9, 24, 3, 8, 22)


@pytest.mark.unit
def test_the_night_the_mac_slept_mid_screen():
    run = parse(FIX / "20260925_021157.log")
    assert run.slept and not run.failures
    assert run.start_late_by is None, "11 minutes after 02:00 is within grace"


def _write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body)
    return p


@pytest.mark.unit
def test_step_timestamps_measure_the_screen_directly(tmp_path):
    log = _write(tmp_path, "20261001_015501.log", "\n".join([
        "=== nightly run 20261001_015501 ===",
        "mandate=equity_value max_names=5 picks=8 controls=4 budget=60",
        "--- step 1: screen (no model calls) [01:55:02] ---",
        "Mandate equity_value as of 2026-10-01 (run 2026-10-01T02:31:10).",
        "--- step 2: adjudicate at most 5 equity_value names [02:31:12] ---",
        "Ran 5 names, skipped 0. Log: x",
        "Model usage: 5 cell(s), 3,000,000 tokens in (of which 2,000,000 cache read, 1 cache write), "
        "90,000 out, 400 calls; per cell 600,000 in / 18,000 out.",
        "--- data store ---",
        "Data store (read_write) /x/store.sqlite: 812.0 MB, 9,000 responses (7,000 final), 40 snapshots.",
        "81% of requests served without a fetch.",
        "=== finished 03:10:00, screen-run exit=0 ===",
    ]))
    run = parse(log)
    assert run.steps["screen"].hour == 1 and run.screen_took.total_seconds() == 36 * 60 + 10
    assert run.problems() == []
    assert run.usage_lines and run.served_line.startswith("81%")
    text = run.summary()
    assert "per cell 600,000 in" in text and "No problems found." in text


@pytest.mark.unit
def test_a_log_that_stops_short_did_not_finish(tmp_path):
    log = _write(tmp_path, "20261002_020000.log",
                 "=== nightly run 20261002_020000 ===\n--- step 1: screen (no model calls) [02:00:01] ---\n")
    assert "did not finish" in parse(log).problems()[0]


@pytest.mark.unit
def test_a_failed_screen_and_a_nonzero_exit_are_named(tmp_path):
    log = _write(tmp_path, "20261003_020000.log", "\n".join([
        "=== nightly run 20261003_020000 ===",
        "screen failed; not starting the agent loop on a stale shortlist.",
        "=== finished 02:05:00, screen-run exit=2 ===",
    ]))
    problems = parse(log).problems()
    assert any("screen failed" in p for p in problems) and "exit code 2" in problems


@pytest.mark.unit
def test_a_job_that_fired_late_is_named(tmp_path):
    log = _write(tmp_path, "20261004_071500.log",
                 "=== nightly run 20261004_071500 ===\n=== finished 07:40:00, screen-run exit=0 ===\n")
    assert any("meant to start at 02:00" in p for p in parse(log).problems())


@pytest.mark.unit
def test_colour_codes_in_older_logs_are_ignored(tmp_path):
    log = _write(tmp_path, "20261005_020000.log",
                 "=== nightly run 20261005_020000 ===\n"
                 "Mandate \x1b[1mequity_value\x1b[0m as of \x1b[1m2026-10-05\x1b[0m (run 2026-10-05T02:40:00).\n"
                 "=== finished 03:00:00, screen-run exit=0 ===\n")
    assert parse(log).work_started == datetime(2026, 10, 5, 2, 40)


@pytest.mark.unit
def test_finding_a_days_log():
    assert log_for(FIX, date(2026, 9, 24)).name == "20260924_020003.log"
    assert log_for(FIX, date(2026, 9, 1)) is None
    assert latest(FIX).name == "20260925_021157.log"


@pytest.mark.unit
def test_a_run_that_never_started_today_is_named():
    from tradingagents.ops.nightly_log import missed_today

    last = FIX / "20260924_020003.log"
    assert "No nightly run has started today" in missed_today(last, datetime(2026, 9, 25, 9, 0))
    assert missed_today(last, datetime(2026, 9, 25, 2, 30)) is None, "not due until an hour after 02:00"
    assert missed_today(last, datetime(2026, 9, 24, 9, 0)) is None, "today's run exists"
