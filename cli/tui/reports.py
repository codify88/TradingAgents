"""The set of reports a run produces, however it is being read.

One model serves both front-ends. A live run fills it section by section from
graph state; a saved run reads it off disk. Keeping them on one type is what
lets the browser and the live view share their widgets, and it keeps the
ordering and the titles from drifting apart between the two.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Display order and titles, keyed by the state field each section comes from.
# The order is the order the pipeline produces them, which is also the order a
# reader wants to move through them.
SECTION_TITLES: dict[str, str] = {
    "market_report": "Market Analysis",
    "sentiment_report": "Social Sentiment",
    "news_report": "News Analysis",
    "fundamentals_report": "Fundamentals Analysis",
    "investment_plan": "Research Team Decision",
    "trader_investment_plan": "Trading Team Plan",
    "final_trade_decision": "Portfolio Management Decision",
}

SECTION_GROUPS: dict[str, str] = {
    "market_report": "Analysts",
    "sentiment_report": "Analysts",
    "news_report": "Analysts",
    "fundamentals_report": "Analysts",
    "investment_plan": "Research",
    "trader_investment_plan": "Trading",
    "final_trade_decision": "Portfolio",
}

# Saved runs are a directory tree; map it back onto the same vocabulary.
_DIR_GROUPS = {
    "1_analysts": "Analysts",
    "2_research": "Research",
    "3_trading": "Trading",
    "4_risk": "Risk",
    "5_portfolio": "Portfolio",
}

_FILE_TITLES = {
    "market": "Market Analysis",
    "sentiment": "Social Sentiment",
    "news": "News Analysis",
    "fundamentals": "Fundamentals Analysis",
    "bull": "Bull Researcher",
    "bear": "Bear Researcher",
    "manager": "Research Manager",
    "trader": "Trading Team Plan",
    "aggressive": "Aggressive Analyst",
    "conservative": "Conservative Analyst",
    "neutral": "Neutral Analyst",
    "decision": "Portfolio Management Decision",
}


@dataclass
class ReportSection:
    """One readable report, present or still pending."""

    key: str
    title: str
    group: str
    content: str | None = None

    @property
    def ready(self) -> bool:
        return bool(self.content and self.content.strip())

    @property
    def label(self) -> str:
        """Tab label: the title, marked when nothing has arrived yet."""
        return self.title if self.ready else f"{self.title} …"


@dataclass
class ReportSet:
    """Ordered sections for one run, live or saved."""

    title: str = ""
    sections: list[ReportSection] = field(default_factory=list)
    _index: dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._reindex()

    def _reindex(self) -> None:
        self._index = {s.key: i for i, s in enumerate(self.sections)}

    def __len__(self) -> int:
        return len(self.sections)

    def __iter__(self):
        return iter(self.sections)

    def get(self, key: str) -> ReportSection | None:
        i = self._index.get(key)
        return self.sections[i] if i is not None else None

    @property
    def ready(self) -> list[ReportSection]:
        return [s for s in self.sections if s.ready]

    def upsert(self, key: str, title: str, group: str, content: str | None) -> bool:
        """Add or update a section. True when the content actually changed.

        The return value is what keeps the live view from rebuilding the pane on
        every graph chunk: the stream re-emits unchanged state constantly, and
        re-rendering a long markdown report several times a second makes the
        pane unreadable and throws away the reader's scroll position.
        """
        existing = self.get(key)
        if existing is None:
            self.sections.append(ReportSection(key, title, group, content))
            self._reindex()
            return bool(content)
        if existing.content == content:
            return False
        existing.content = content
        return True


def live_report_set(
    sections: dict, mandate_analysts: tuple = (), title: str = ""
) -> ReportSet:
    """Sections for a run in flight, in pipeline order.

    ``sections`` is the run's report map -- only the analysts actually selected
    appear in it, so an unselected one must not show up as a tab that stays
    pending for the whole run. Mandate analysts are spliced in after the
    upstream ones, matching where they run in the graph and where the saved
    tree puts them.
    """
    reports = ReportSet(title=title)
    for key, upstream_title in SECTION_TITLES.items():
        if key == "investment_plan":
            # Declared up front, content or not: the reader can see what is
            # still coming instead of watching tabs appear at random.
            for mkey, label in mandate_analysts:
                section_key = f"mandate:{mkey}"
                reports.upsert(section_key, label, "Analysts", sections.get(section_key))
        if key in sections:
            reports.upsert(key, upstream_title, SECTION_GROUPS[key], sections[key])
    return reports


def saved_report_set(run_dir: Path) -> ReportSet:
    """Sections for a finished run, read from its report tree."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise FileNotFoundError(f"no report tree at {run_dir}")

    reports = ReportSet(title=run_dir.name)
    for group_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        group = _DIR_GROUPS.get(group_dir.name, group_dir.name)
        for path in sorted(group_dir.glob("*.md")):
            stem = path.stem
            title = _FILE_TITLES.get(stem, stem.replace("_", " ").title())
            reports.upsert(
                f"{group_dir.name}/{stem}", title, group,
                path.read_text(encoding="utf-8"),
            )

    complete = run_dir / "complete_report.md"
    if complete.is_file():
        reports.upsert(
            "complete", "Complete Report", "All",
            complete.read_text(encoding="utf-8"),
        )
    if not len(reports):
        raise FileNotFoundError(f"no markdown reports under {run_dir}")
    return reports
