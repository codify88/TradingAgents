"""The book: what the strategy holds, by cohort, and whether it may trade.

One file (``trading/book.json``) with every live cohort -- the names bought at
one open, each held ``hold_sessions`` sessions -- plus two switches: ``halted``
(the operator's kill switch) and ``blocked`` (a reconciliation mismatch nobody
has acknowledged). Either one refuses every new order. Every change is also
appended to ``events.jsonl``, so the history survives the file being rewritten.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path


def trading_dir(config: dict) -> Path:
    path = Path(config.get("trading_dir") or Path(config["data_cache_dir"]).parent / "trading")
    (path / "plans").mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Cohort:
    plan_id: str
    strategy: str
    entry_session: str
    exit_session: str
    planned: dict[str, int]                         # symbol -> shares ordered
    held: dict[str, float] = field(default_factory=dict)   # symbol -> shares filled (reconciled)
    exit_plan: str | None = None                    # the plan that sells it
    closed: bool = False
    reconciled: bool = False                        # held reflects the broker's fills

    def shares(self) -> dict[str, float]:
        """What the cohort holds: its fills once reconciled, its orders before."""
        return dict(self.held) if self.reconciled else {**{k: float(v) for k, v in self.planned.items()},
                                                         **self.held}


@dataclass
class Book:
    cohorts: list[Cohort] = field(default_factory=list)
    halted: bool = False
    halted_reason: str = ""
    blocked: list[str] = field(default_factory=list)
    last_reconciled: str = ""
    applied: list[str] = field(default_factory=list)  # client order ids already booked

    @property
    def live(self) -> list[Cohort]:
        return [c for c in self.cohorts if not c.closed]

    def refusal(self) -> str | None:
        """Why no order may be placed now, or None."""
        if self.halted:
            return f"trading is halted ({self.halted_reason or 'no reason given'}); `tradingagents trade resume` lifts it"
        if self.blocked:
            return ("reconciliation found mismatches nobody has acknowledged: " + "; ".join(self.blocked)
                    + ". Check the broker, then `tradingagents trade ack`")
        return None


def load_book(config: dict) -> Book:
    path = trading_dir(config) / "book.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return Book()
    cohorts = [Cohort(**c) for c in data.pop("cohorts", [])]
    return Book(cohorts=cohorts, **data)


def save_book(config: dict, book: Book, event: str, **detail) -> None:
    d = trading_dir(config)
    tmp = d / "book.json.tmp"
    tmp.write_text(json.dumps(asdict(book), indent=2))
    tmp.replace(d / "book.json")
    with (d / "events.jsonl").open("a") as fh:
        fh.write(json.dumps({"at": datetime.now(UTC).isoformat(timespec="seconds"),
                             "event": event, **detail}) + "\n")


def events(config: dict, limit: int = 50) -> list[dict]:
    try:
        lines = (trading_dir(config) / "events.jsonl").read_text().splitlines()
    except OSError:
        return []
    return [json.loads(x) for x in lines[-limit:] if x.strip()]
