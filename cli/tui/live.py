"""The live run view: watch the pipeline, read any report while it builds.

The graph stream runs on a worker thread and keeps mutating the existing
``MessageBuffer``; the app polls that buffer on a timer. Polling rather than
rewiring every update path is deliberate -- the buffer is already the single
place every status, message and report lands, and reaching into the stream loop
to push events would mean touching ``cli/main.py`` in a dozen places, on a file
that conflicts on every upstream merge.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Static

from .reports import live_report_set
from .widgets import ReportPane

TEAM_ORDER = [
    ("Analyst Team", "analysts"),
    ("Research Team", "research"),
    ("Trading Team", "trading"),
    ("Risk Management", "risk"),
    ("Portfolio Management", "portfolio"),
]

STATUS_STYLE = {
    "pending": "yellow",
    "in_progress": "bold cyan",
    "completed": "green",
    "error": "bold red",
}


class ProgressPanel(DataTable):
    """Team / agent / status, the same grouping the rich view used."""

    def on_mount(self) -> None:
        self.cursor_type = "none"
        self.zebra_stripes = False
        self.add_columns("Team", "Agent", "Status")

    def sync(self, buffer) -> None:
        teams = {
            "Analyst Team": [
                "Market Analyst", "Sentiment Analyst", "News Analyst",
                "Fundamentals Analyst",
                *(label for _, label in buffer.mandate_analysts),
            ],
            "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
            "Trading Team": ["Trader"],
            "Risk Management": [
                "Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
            "Portfolio Management": ["Portfolio Manager"],
        }
        rows = []
        for team, agents in teams.items():
            active = [a for a in agents if a in buffer.agent_status]
            for i, agent in enumerate(active):
                status = buffer.agent_status.get(agent, "pending")
                style = STATUS_STYLE.get(status, "white")
                rows.append((team if i == 0 else "", agent,
                             f"[{style}]{status}[/{style}]"))
        if rows == getattr(self, "_rows_cache", None):
            return
        self.clear()
        for row in rows:
            self.add_row(*row)
        self._rows_cache = rows


class MessagesPanel(DataTable):
    """The tail of the message and tool-call stream."""

    LIMIT = 60

    def on_mount(self) -> None:
        self.cursor_type = "none"
        self.add_columns("Time", "Type", "Content")

    def sync(self, buffer) -> None:
        try:
            messages = list(buffer.messages)[-self.LIMIT:]
            tools = list(buffer.tool_calls)[-self.LIMIT:]
        except RuntimeError:
            # The worker appends while we read. Skipping a frame is invisible at
            # four frames a second, and the next tick catches up.
            return
        combined = sorted(
            [(t, "Tool", f"{name}({args})") for t, name, args in tools]
            + [(t, kind, content) for t, kind, content in messages],
            key=lambda row: row[0],
        )[-self.LIMIT:]
        if combined == getattr(self, "_rows_cache", None):
            return
        self.clear()
        for stamp, kind, content in combined:
            text = " ".join(str(content).split())
            self.add_row(stamp, kind, text[:400])
        self._rows_cache = combined
        if combined:
            self.scroll_end(animate=False)


class StatsBar(Static):
    """The footer line: agents, calls, tokens, reports, elapsed."""

    def sync(self, buffer, stats_handler, start_time) -> None:
        done = sum(1 for s in buffer.agent_status.values() if s == "completed")
        parts = [f"Agents: {done}/{len(buffer.agent_status)}"]
        if stats_handler is not None:
            parts += [
                f"LLM: {stats_handler.llm_calls}",
                f"Tools: {stats_handler.tool_calls}",
                f"Tokens: {stats_handler.prompt_tokens/1000:.1f}k↑ "
                f"{stats_handler.completion_tokens/1000:.1f}k↓",
            ]
        parts.append(
            f"Reports: {buffer.get_completed_reports_count()}/{len(buffer.report_sections)}")
        if start_time is not None:
            import time

            elapsed = int(time.time() - start_time)
            parts.append(f"⏱ {elapsed // 60}:{elapsed % 60:02d}")
        self.update(" | ".join(parts))


class LiveRunApp(App):
    """Progress and messages above, a readable report pane below."""

    TITLE = "TradingAgents"
    CSS = """
    #top { height: 40%; }
    #progress { width: 40%; border: round $primary; }
    #messages { width: 60%; border: round $accent; }
    ReportPane { border: round $success; height: 1fr; }
    StatsBar { dock: bottom; height: 1; content-align: center middle; color: $text-muted; }
    """

    BINDINGS = [
        Binding("left,[", "prev", "Prev report"),
        Binding("right,]", "next", "Next report"),
        Binding("f", "follow", "Follow latest"),
        Binding("g", "top", "Top"),
        Binding("G", "bottom", "Bottom"),
        Binding("ctrl+c", "interrupt", "Stop run"),
    ]

    def __init__(self, stream_fn, buffer, stats_handler=None, start_time=None,
                 subtitle: str = ""):
        super().__init__()
        self._stream_fn = stream_fn
        self._buffer = buffer
        self._stats = stats_handler
        self._start = start_time
        self.sub_title = subtitle
        self.result = None
        self.error: BaseException | None = None
        # Content seen at the last poll, so "latest" means most recently
        # *arrived* rather than last in display order -- the pipeline does not
        # finish sections in the order they are listed.
        self._seen: dict[str, int] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="top"):
            yield ProgressPanel(id="progress")
            yield MessagesPanel(id="messages")
        yield ReportPane(id="reports")
        yield StatsBar()
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._run_stream, thread=True, name="graph")
        self.set_interval(0.25, self._pull)

    def _run_stream(self) -> None:
        try:
            self.result = self._stream_fn(lambda *a, **k: None)
        except BaseException as exc:  # surfaced by the caller, not swallowed
            self.error = exc
        finally:
            self.call_from_thread(self.exit)

    @property
    def pane(self) -> ReportPane:
        return self.query_one("#reports", ReportPane)

    def _pull(self) -> None:
        """Refresh every panel from the buffer the worker is writing to."""
        buffer = self._buffer
        self.query_one(ProgressPanel).sync(buffer)
        self.query_one(MessagesPanel).sync(buffer)
        self.query_one(StatsBar).sync(buffer, self._stats, self._start)

        reports = live_report_set(
            dict(buffer.report_sections), tuple(buffer.mandate_analysts)
        )
        newest = None
        for section in reports:
            if not section.ready:
                continue
            fingerprint = hash(section.content)
            if self._seen.get(section.key) != fingerprint:
                self._seen[section.key] = fingerprint
                newest = section.key

        pane = self.pane
        pane.refresh_reports(reports)
        if pane.following and newest and pane.active_key != newest:
            pane.show(newest)

    # --- actions ----------------------------------------------------------

    def action_prev(self) -> None:
        self.pane.step(-1)

    def action_next(self) -> None:
        self.pane.step(1)

    def action_follow(self) -> None:
        self.pane.follow_latest()

    def action_top(self) -> None:
        self.pane.query_one("#report-scroll").scroll_home(animate=False)

    def action_bottom(self) -> None:
        self.pane.query_one("#report-scroll").scroll_end(animate=False)

    def action_interrupt(self) -> None:
        self.exit()


def run_live(stream_fn, buffer, stats_handler=None, start_time=None, subtitle=""):
    """Drive a run under the live view; return whatever the stream returned."""
    app = LiveRunApp(stream_fn, buffer, stats_handler, start_time, subtitle)
    app.run()
    if app.error is not None:
        raise app.error
    return app.result


__all__ = ["LiveRunApp", "run_live"]
