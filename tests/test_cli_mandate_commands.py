"""The CLI carries the mandate from screen to backtest.

The screener's whole claim -- that its shortlist beats a random control -- is
tested by running both through the loop under the mandate that picked them.
If the mandate is dropped on the way, that test measures the wrong thing.
"""

import re
import subprocess
import sys
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import cli.main as m

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(output: str) -> str:
    """Console output as plain, single-spaced text, so assertions test content.

    Two ambient things leak into `result.output`, and pytest isolates neither:

    *Styling.* rich decides whether to style from FORCE_COLOR / COLORTERM /
    TERM, so `"Mandate: x" in output` holds in CI and fails in a colour-capable
    shell, where the line arrives as "\x1b[36mMandate:\x1b[0m x". NO_COLOR is
    not enough by itself -- it drops colour but keeps rich's bold highlighting
    on numbers, so a date still arrives as "\x1b[1m2026\x1b[0m-...".

    *Width.* rich wraps to the console width it detected when the Console was
    built, which is at import, so the `COLUMNS` that CliRunner passes into the
    invocation arrives too late. Under a narrow terminal the asserted command
    breaks across lines mid-string.

    Collapsing whitespace after stripping the escapes makes both invisible.
    """
    return " ".join(_ANSI.sub("", output).split())


@pytest.fixture
def captured(monkeypatch, tmp_path):
    calls = {}

    def fake_run_backtest(tickers, dates, config, **kwargs):
        calls.update(tickers=tickers, dates=dates, **kwargs)
        return SimpleNamespace(log_path=tmp_path / "log.md", cells_run=0, skipped=0,
                               failures=[], settlement_failures=[])

    monkeypatch.setattr(m, "run_backtest", fake_run_backtest)
    monkeypatch.setattr(m, "summarize", lambda log: SimpleNamespace(render=lambda: ""))
    return calls


@pytest.mark.unit
def test_backtest_passes_the_mandate_through(captured):
    result = runner.invoke(m.app, ["backtest", "KO", "--start", "2025-01-06",
                                   "--end", "2025-01-06", "--mandate", "equity_value"])
    assert result.exit_code == 0, result.output
    assert captured["mandate"] == "equity_value"
    assert "Mandate: equity_value" in plain(result.output)


@pytest.mark.unit
def test_backtest_without_the_flag_defers_to_the_environment(captured, monkeypatch):
    monkeypatch.setitem(m.DEFAULT_CONFIG, "mandate", "equity_momentum")
    result = runner.invoke(m.app, ["backtest", "KO", "--start", "2025-01-06", "--end", "2025-01-06"])
    assert result.exit_code == 0, result.output
    assert captured["mandate"] is None
    assert "Mandate: equity_momentum" in plain(result.output)


@pytest.mark.unit
def test_the_command_screen_prints_keeps_the_mandate(monkeypatch):
    import tradingagents.screener as screener
    import tradingagents.screener.review as review

    manifest = SimpleNamespace(picks=["x"], pick_symbols=["KO", "PEP"], control_symbols=["XOM"],
                               run_id="2026-09-18_equity_value_101500")
    monkeypatch.setattr(screener, "run_screen",
                        lambda *a, **k: SimpleNamespace(manifest=manifest, excluded=[]))
    monkeypatch.setattr(screener, "save_manifest", lambda *a: "manifest.json")
    monkeypatch.setattr(review, "render_screen", lambda *a: "report")

    result = runner.invoke(m.app, ["screen", "--mandate", "equity_value", "--date", "2026-09-18"],
                           env={"COLUMNS": "400"})
    assert result.exit_code == 0, result.output
    assert ("tradingagents backtest KO,PEP,XOM --start 2026-09-18 --end 2026-09-18 "
            "--mandate equity_value") in plain(result.output)
    assert "tradingagents screen-run 101500" in plain(result.output)


@pytest.mark.unit
def test_module_entry_point_registers_every_command():
    """`python -m cli.main` must not start the app before the later commands exist."""
    out = subprocess.run([sys.executable, "-m", "cli.main", "--help"], capture_output=True,
                         text=True, env={"COLUMNS": "200", "NO_COLOR": "1", "PATH": ""})
    for command in ("backtest", "screen", "screen-review"):
        assert command in out.stdout, out.stdout + out.stderr
