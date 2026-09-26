"""A Desk server for the end-to-end tests: the real API over a temporary book.

The broker is the tests' FakeBroker (tests/test_trading.py), a pending plan is
built for the next open, and the enrolment code is written to e2e/.enrol-code
for the test to read. Nothing touches the real trading directory or Alpaca.

    ../.venv/bin/python e2e/server.py --port 8799
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from tests.test_trading import FakeBroker  # noqa: E402
from tradingagents.desk.app import create_app  # noqa: E402
from tradingagents.desk.passkeys import Passkeys  # noqa: E402
from tradingagents.trading import plan as pl  # noqa: E402
from tradingagents.trading.book import load_book  # noqa: E402


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="desk-e2e-"))
    config = {"data_cache_dir": str(tmp / "cache"), "results_dir": str(tmp / "results"),
              "trading_dir": str(tmp / "trading")}
    broker = FakeBroker()

    # A plan for the next open: planned at 07:00 New York on the next weekday,
    # so its 09:28 cutoff is hours away whenever the tests run.
    now = datetime.now(pl.NEW_YORK)
    d = now.date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    planned_at = datetime.combine(d, time(7, 0), pl.NEW_YORK) if now.time() >= pl.OPG_CUTOFF else now.replace(hour=7, minute=0)
    picks = [("ACAD", 1.0), ("ABNB", 0.5), ("CRWD", 1.0)]
    with mock.patch.object(pl, "entries", return_value=(picks, ["e2e fixture"])):
        plan = pl.build(config, broker, load_book(config), now=planned_at, price=lambda s: 100.0)
    pl.save_plan(config, plan)

    keys = Passkeys(config)
    (HERE / ".enrol-code").write_text(keys.new_code())
    app = create_app(config, broker_factory=lambda cfg: broker, passkeys=keys)
    print(f"e2e Desk on http://localhost:{args.port}/ with plan {plan.id}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
