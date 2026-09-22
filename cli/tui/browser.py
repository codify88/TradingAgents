"""Browse a finished run's reports.

The same pane the live view uses, over a report tree on disk. Useful in its own
right, and it is how the pane gets developed and tested without waiting out a
seven-minute run.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView

from .reports import ReportSet, saved_report_set
from .widgets import ReportPane


def list_runs(results_dir) -> list[Path]:
    """Saved report trees, newest first."""
    root = Path(results_dir) / "reports"
    if not root.is_dir():
        return []
    runs = [p for p in root.iterdir() if p.is_dir() and any(p.glob("**/*.md"))]
    return sorted(runs, key=lambda p: p.stat().st_mtime, reverse=True)


def resolve_run(results_dir, name: str) -> Path:
    """A run directory from a path, a full name, or a unique prefix."""
    direct = Path(name).expanduser()
    if direct.is_dir():
        return direct
    runs = list_runs(results_dir)
    for run in runs:
        if run.name == name:
            return run
    matches = [r for r in runs if r.name.startswith(name) or name in r.name]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(f"no saved run matching {name!r}")
    names = "\n  ".join(r.name for r in matches[:8])
    raise FileNotFoundError(f"{name!r} matches several runs:\n  {names}")


class ReportBrowser(App):
    """Read one run's reports, switching sections and scrolling freely."""

    TITLE = "TradingAgents"
    CSS = "Screen { layers: base; }"

    BINDINGS = [
        Binding("q,escape", "quit", "Quit"),
        Binding("left,[", "prev", "Prev report"),
        Binding("right,]", "next", "Next report"),
        Binding("g", "top", "Top"),
        Binding("G", "bottom", "Bottom"),
    ]

    def __init__(self, reports: ReportSet):
        super().__init__()
        self._reports = reports
        self.sub_title = reports.title

    def compose(self) -> ComposeResult:
        yield Header()
        yield ReportPane(self._reports, id="reports")
        yield Footer()

    @property
    def pane(self) -> ReportPane:
        return self.query_one("#reports", ReportPane)

    def action_prev(self) -> None:
        self.pane.step(-1)

    def action_next(self) -> None:
        self.pane.step(1)

    def action_top(self) -> None:
        self.pane.query_one("#report-scroll").scroll_home(animate=False)

    def action_bottom(self) -> None:
        self.pane.query_one("#report-scroll").scroll_end(animate=False)


class RunPicker(Screen):
    """Choose among saved runs when none was named."""

    BINDINGS = [Binding("q,escape", "app.quit", "Quit")]

    def __init__(self, runs: list[Path]):
        super().__init__()
        self._runs = runs

    def compose(self) -> ComposeResult:
        yield Header()
        yield ListView(*[ListItem(Label(r.name), id=f"r{i}")
                         for i, r in enumerate(self._runs)])
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        run = self._runs[int(event.item.id[1:])]
        self.app.sub_title = run.name
        self.app.push_screen(ReportScreen(saved_report_set(run)))


class ReportScreen(Screen):
    """The report pane as a pushable screen, so the picker can sit behind it."""

    BINDINGS = list(ReportBrowser.BINDINGS)

    def __init__(self, reports: ReportSet):
        super().__init__()
        self._reports = reports

    def compose(self) -> ComposeResult:
        yield Header()
        yield ReportPane(self._reports, id="reports")
        yield Footer()

    @property
    def pane(self) -> ReportPane:
        return self.query_one("#reports", ReportPane)

    def action_prev(self) -> None:
        self.pane.step(-1)

    def action_next(self) -> None:
        self.pane.step(1)

    def action_top(self) -> None:
        self.pane.query_one("#report-scroll").scroll_home(animate=False)

    def action_bottom(self) -> None:
        self.pane.query_one("#report-scroll").scroll_end(animate=False)


class RunPickerApp(App):
    """Pick a run, then read it."""

    TITLE = "TradingAgents"
    BINDINGS = [Binding("q,escape", "quit", "Quit")]

    def __init__(self, runs: list[Path]):
        super().__init__()
        self._runs = runs
        self.sub_title = f"{len(runs)} saved run(s)"

    def on_mount(self) -> None:
        self.push_screen(RunPicker(self._runs))


def browse(run_dir: Path) -> None:
    ReportBrowser(saved_report_set(run_dir)).run()


def pick_and_browse(results_dir) -> None:
    runs = list_runs(results_dir)
    if not runs:
        raise FileNotFoundError(f"no saved runs under {Path(results_dir) / 'reports'}")
    if len(runs) == 1:
        browse(runs[0])
    else:
        RunPickerApp(runs).run()
