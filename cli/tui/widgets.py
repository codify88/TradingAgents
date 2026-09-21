"""The report pane: pick a report, scroll it, without losing your place.

Used unchanged by the live run view and by the saved-run browser.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Markdown, Static, Tabs
from textual.widgets._tabs import Tab

from .reports import ReportSet

PLACEHOLDER = "*Waiting for this report…*"


class ReportPane(Static):
    """Tabs over a scrollable markdown view."""

    DEFAULT_CSS = """
    ReportPane { height: 1fr; }
    ReportPane > Tabs { dock: top; }
    ReportPane > VerticalScroll { height: 1fr; padding: 0 1; }
    """

    def __init__(self, reports: ReportSet | None = None, **kwargs):
        super().__init__(**kwargs)
        self._reports = reports or ReportSet()
        self._keys: list[str] = []
        self._active: str | None = None
        # Tab ids must be identifiers, section keys are not, so the mapping back
        # is kept per pane -- a module-level map would be shared by every pane
        # in the process and resolve the wrong report.
        self._tab_keys: dict[str, str] = {}
        # Setting Tabs.active fires TabActivated exactly as a click does, so
        # without this the pane's own programmatic switches would be read as the
        # reader taking control and would cancel following.
        self._setting_tab = False
        # True until the reader picks a tab themselves. A live run keeps moving
        # the view to whatever just arrived, which is the right default and the
        # wrong thing to keep doing the moment someone navigates deliberately.
        self.following = True

    def compose(self) -> ComposeResult:
        yield Tabs()
        # The scroll container holds focus, not the tab bar: that way up/down,
        # PgUp/PgDn, Home/End are handled natively where the reader expects,
        # and left/right fall through to the app's report-switching bindings
        # instead of being eaten by Tabs.
        scroll = VerticalScroll(Markdown(), id="report-scroll")
        scroll.can_focus = True
        yield scroll

    def on_mount(self) -> None:
        self.refresh_reports(self._reports)
        self.query_one("#report-scroll", VerticalScroll).focus()

    # --- state ------------------------------------------------------------

    @property
    def active_key(self) -> str | None:
        return self._active

    def refresh_reports(self, reports: ReportSet) -> None:
        """Adopt a new snapshot, rebuilding only what actually changed."""
        self._reports = reports
        keys = [s.key for s in reports]
        if keys != self._keys:
            self._rebuild_tabs(keys)
        self._render_active()

    def _rebuild_tabs(self, keys: list[str]) -> None:
        tabs = self.query_one(Tabs)
        tabs.clear()
        self._tab_keys.clear()
        for section in self._reports:
            tab_id = _tab_id(section.key)
            self._tab_keys[tab_id] = section.key
            tabs.add_tab(Tab(section.label, id=tab_id))
        self._keys = keys
        if self._active not in keys:
            self._active = keys[0] if keys else None
        if self._active:
            self._setting_tab = True
            try:
                tabs.active = _tab_id(self._active)
            finally:
                self._setting_tab = False

    def show(self, key: str, *, manual: bool = False) -> None:
        if key not in self._keys:
            return
        if manual:
            self.following = False
        self._active = key
        tabs = self.query_one(Tabs)
        if tabs.active != _tab_id(key):
            self._setting_tab = True
            try:
                tabs.active = _tab_id(key)
            finally:
                self._setting_tab = False
        self._render_active()
        # Activating a tab moves focus to the tab bar; give it back so the very
        # next keystroke scrolls the report the reader just switched to.
        scroll = self.query_one("#report-scroll", VerticalScroll)
        scroll.scroll_home(animate=False)
        scroll.focus()

    def follow_latest(self) -> None:
        """Jump to the newest ready report and resume following it."""
        self.following = True
        ready = self._reports.ready
        if ready:
            self.show(ready[-1].key)

    def _render_active(self) -> None:
        section = self._reports.get(self._active) if self._active else None
        markdown = self.query_one(Markdown)
        body = section.content if (section and section.ready) else PLACEHOLDER
        if getattr(markdown, "_ta_body", None) == body:
            return  # unchanged: re-rendering would throw away the scroll position
        scroll = self.query_one("#report-scroll", VerticalScroll)
        offset = scroll.scroll_offset.y
        markdown.update(body)
        markdown._ta_body = body
        # A report that is still being written grows under the reader. Holding
        # the offset keeps the passage they were reading in place instead of
        # snapping to the top on every update.
        if offset:
            scroll.scroll_to(y=offset, animate=False)

    # --- navigation -------------------------------------------------------

    def step(self, delta: int) -> None:
        if not self._keys:
            return
        i = self._keys.index(self._active) if self._active in self._keys else 0
        self.show(self._keys[(i + delta) % len(self._keys)], manual=True)

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        if self._setting_tab:
            return
        key = self._tab_keys.get(event.tab.id or "")
        if key and key != self._active:
            self.show(key, manual=True)


def _tab_id(key: str) -> str:
    """Tab ids must be valid identifiers; section keys contain / and :."""
    return "t_" + "".join(c if c.isalnum() else "_" for c in key)
