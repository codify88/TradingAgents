"""The Textual front-end: report model, pane behaviour, live view.

The pane is shared by the live run and the saved-run browser, so most of this
exercises it through the browser -- same widget, no seven-minute run to wait on.
"""

import asyncio
import threading

import pytest

from cli.tui.browser import ReportBrowser, list_runs, resolve_run
from cli.tui.live import LiveRunApp
from cli.tui.reports import ReportSet, live_report_set, saved_report_set

SIZE = (140, 44)


def _write_run(tmp_path, name="AAPL_20260101_equity_value"):
    run = tmp_path / "reports" / name
    for rel, text in [
        ("1_analysts/market.md", "# Market\n" + "\n\n".join(f"para {i}" for i in range(80))),
        ("1_analysts/quality.md", "# Quality\nROIC 13.9%"),
        ("5_portfolio/decision.md", "# Decision\nHold"),
        ("complete_report.md", "# Everything"),
    ]:
        path = run / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return run


# --- the model -------------------------------------------------------------


class TestReportSet:
    def test_upsert_reports_whether_content_actually_changed(self):
        """The live view polls constantly and the stream re-emits unchanged
        state; re-rendering on every tick would throw away the scroll position."""
        reports = ReportSet()
        assert reports.upsert("k", "K", "G", "body") is True
        assert reports.upsert("k", "K", "G", "body") is False
        assert reports.upsert("k", "K", "G", "new") is True

    def test_a_section_with_no_content_is_not_ready(self):
        reports = ReportSet()
        reports.upsert("k", "K", "G", None)
        reports.upsert("blank", "B", "G", "   ")
        assert reports.ready == []
        assert reports.get("k").label.endswith("…")

    def test_saved_runs_are_read_in_pipeline_order(self, tmp_path):
        reports = saved_report_set(_write_run(tmp_path))
        assert [s.group for s in reports][:2] == ["Analysts", "Analysts"]
        assert reports.get("complete") is not None
        assert all(s.ready for s in reports)

    def test_a_directory_without_reports_is_an_error_not_an_empty_view(self, tmp_path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(FileNotFoundError):
            saved_report_set(tmp_path / "empty")

    def test_an_unselected_analyst_is_not_offered_as_a_pending_tab(self):
        """report_sections only holds the analysts the run selected. Listing the
        rest would leave tabs that stay pending for the whole run."""
        reports = live_report_set({"market_report": "M", "final_trade_decision": None})
        assert [s.key for s in reports] == ["market_report", "final_trade_decision"]

    def test_a_mandates_analysts_are_declared_before_they_produce(self):
        reports = live_report_set({"market_report": "M"},
                                  mandate_analysts=(("quality", "Quality Analyst"),))
        keys = [s.key for s in reports]
        assert "mandate:quality" in keys
        assert keys.index("market_report") < keys.index("mandate:quality")
        assert not reports.get("mandate:quality").ready


# --- run discovery ---------------------------------------------------------


class TestRunDiscovery:
    def test_runs_are_listed_newest_first(self, tmp_path):
        import os
        import time
        _write_run(tmp_path, "A_1")
        time.sleep(0.01)
        b = _write_run(tmp_path, "B_2")
        os.utime(b, None)
        assert list_runs(tmp_path)[0].name == "B_2"

    def test_a_unique_prefix_resolves(self, tmp_path):
        _write_run(tmp_path, "AAPL_20260101_equity_value")
        assert resolve_run(tmp_path, "AAPL_2026").name == "AAPL_20260101_equity_value"

    def test_an_ambiguous_name_lists_the_candidates_rather_than_guessing(self, tmp_path):
        _write_run(tmp_path, "AAPL_a")
        _write_run(tmp_path, "AAPL_b")
        with pytest.raises(FileNotFoundError, match="matches several"):
            resolve_run(tmp_path, "AAPL")

    def test_an_unknown_name_is_an_error(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="no saved run"):
            resolve_run(tmp_path, "NOPE")


# --- the pane, driven through the browser ----------------------------------


def _browse(tmp_path):
    return ReportBrowser(saved_report_set(_write_run(tmp_path)))


class TestReportPane:
    def test_switching_reports_and_scrolling(self, tmp_path):
        async def scenario():
            app = _browse(tmp_path)
            async with app.run_test(size=SIZE) as pilot:
                pane, scroll = app.pane, app.pane.query_one("#report-scroll")
                first = pane.active_key
                await pilot.press("right")
                await pilot.pause()
                assert pane.active_key != first
                await pilot.press("left")
                await pilot.pause()
                assert pane.active_key == first
                # Focus sits on the scroll area, so scrolling keys land there.
                await pilot.press("pagedown")
                await pilot.pause()
                assert scroll.scroll_offset.y > 0
                await pilot.press("g")
                await pilot.pause()
                assert scroll.scroll_offset.y == 0
        asyncio.run(scenario())

    def test_switching_report_starts_the_new_one_at_the_top(self, tmp_path):
        async def scenario():
            app = _browse(tmp_path)
            async with app.run_test(size=SIZE) as pilot:
                scroll = app.pane.query_one("#report-scroll")
                await pilot.press("pagedown")
                await pilot.pause()
                assert scroll.scroll_offset.y > 0
                await pilot.press("right")
                await pilot.pause()
                assert scroll.scroll_offset.y == 0
        asyncio.run(scenario())

    def test_the_pane_keeps_its_place_when_content_is_unchanged(self, tmp_path):
        """A live report is re-supplied on every poll; re-rendering identical
        content would snap the reader back to the top several times a second."""
        async def scenario():
            app = _browse(tmp_path)
            async with app.run_test(size=SIZE) as pilot:
                pane, scroll = app.pane, app.pane.query_one("#report-scroll")
                await pilot.press("pagedown")
                await pilot.pause()
                offset = scroll.scroll_offset.y
                pane.refresh_reports(pane._reports)
                await pilot.pause()
                assert scroll.scroll_offset.y == offset
        asyncio.run(scenario())


# --- the live view ---------------------------------------------------------


class TestLiveView:
    def _app(self, sections, mandate=()):
        from cli.main import MessageBuffer

        buffer = MessageBuffer()
        analysts = tuple(type("A", (), {"key": k, "label": lbl}) for k, lbl in mandate)
        buffer.init_for_analysis(sections, analysts)
        return buffer

    def test_it_follows_the_report_that_most_recently_arrived(self):
        """Not the last in display order: the pipeline does not finish sections
        in the order they are listed."""
        buffer = self._app(["market", "news"])
        gate = threading.Event()

        def stream(redraw):
            for key in ("news_report", "market_report"):
                gate.wait()
                gate.clear()
                buffer.update_report_section(key, f"# {key}\nbody")
            gate.wait()
            return {"ok": True}

        async def scenario():
            app = LiveRunApp(stream, buffer)
            async with app.run_test(size=SIZE) as pilot:
                await pilot.pause(0.3)
                for key in ("news_report", "market_report"):
                    gate.set()
                    await pilot.pause(0.4)
                    assert app.pane.active_key == key, key
                gate.set()
                await pilot.pause(0.4)
            assert app.result == {"ok": True}
            assert app.error is None
        asyncio.run(scenario())

    def test_choosing_a_report_stops_the_view_moving_under_you(self):
        buffer = self._app(["market", "news"])
        gate = threading.Event()

        def stream(redraw):
            # clear() after each wait: an Event stays set once set, so without
            # it one set() releases every wait and the run finishes while the
            # test is still driving the app.
            buffer.update_report_section("market_report", "# M\nbody")
            gate.wait()
            gate.clear()
            buffer.update_report_section("news_report", "# N\nbody")
            gate.wait()
            gate.clear()
            return {}

        async def scenario():
            app = LiveRunApp(stream, buffer)
            async with app.run_test(size=SIZE) as pilot:
                await pilot.pause(0.4)
                await pilot.press("right")
                await pilot.pause(0.2)
                chosen = app.pane.active_key
                assert app.pane.following is False
                gate.set()
                await pilot.pause(0.5)
                assert app.pane.active_key == chosen, "a new report stole the view"
                await pilot.press("f")
                await pilot.pause(0.3)
                assert app.pane.following is True
                gate.set()
                await pilot.pause(0.3)
        asyncio.run(scenario())

    def test_a_failing_run_surfaces_its_error_rather_than_exiting_quietly(self):
        buffer = self._app(["market"])

        def stream(redraw):
            raise RuntimeError("graph blew up")

        async def scenario():
            app = LiveRunApp(stream, buffer)
            async with app.run_test(size=SIZE) as pilot:
                await pilot.pause(0.4)
            assert isinstance(app.error, RuntimeError)
        asyncio.run(scenario())


class TestInterruptedRun:
    """Stopping a run must not lose the reports it already paid for.

    The first Textual version returned None on interrupt and cli/main.py called
    .get() on it, so stopping a run ended in an AttributeError -- past the point
    where the decision was logged but before the prompt that writes the report
    tree. The reports were produced, and then discarded on the way out.
    """

    def test_interrupting_is_reported_not_mistaken_for_completion(self):
        from cli.main import MessageBuffer

        buffer = MessageBuffer()
        buffer.init_for_analysis(["market"], ())
        running = threading.Event()

        def stream(redraw):
            buffer.update_report_section("market_report", "# Market\npartial")
            running.wait(timeout=10)
            return {"final_trade_decision": "Rating: Buy"}

        async def scenario():
            app = LiveRunApp(stream, buffer)
            async with app.run_test(size=SIZE) as pilot:
                await pilot.pause(0.4)
                await pilot.press("ctrl+c")
                await pilot.pause(0.4)
            assert app.interrupted is True
            assert app.result is None
            assert app.error is None

        asyncio.run(scenario())
        running.set()

    def test_a_completed_run_is_not_marked_interrupted(self):
        from cli.main import MessageBuffer

        buffer = MessageBuffer()
        buffer.init_for_analysis(["market"], ())

        async def scenario():
            app = LiveRunApp(lambda redraw: {"ok": True}, buffer)
            async with app.run_test(size=SIZE) as pilot:
                await pilot.pause(0.5)
            assert app.interrupted is False
            assert app.result == {"ok": True}

        asyncio.run(scenario())

    def test_partial_reports_can_still_be_written(self, tmp_path):
        """What the interrupt path offers to save."""
        from cli.main import save_report_to_disk

        partial = {"market_report": "# Market\nbody", "news_report": "# News\nbody"}
        out = save_report_to_disk(partial, "AAPL", tmp_path / "AAPL_partial")
        assert out.exists()
        written = {p.name for p in (tmp_path / "AAPL_partial").rglob("*.md")}
        assert "market.md" in written and "news.md" in written
