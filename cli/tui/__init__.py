"""Textual front-end for the CLI: a live run view and a saved-run browser.

Both are the same two widgets over the same data model. A run in flight and a
run finished last week differ only in whether sections are still arriving, so
the browser doubles as the fast way to develop and test the live view -- the
alternative being a seven-minute round trip per change.
"""

from .reports import ReportSection, ReportSet

__all__ = ["ReportSection", "ReportSet"]
