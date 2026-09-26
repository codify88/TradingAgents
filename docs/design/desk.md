# Desk: the operator's local web UI

Status: v1 built 2026-09-25. Builds on `go-live.md` (the lab, paper trading)
and `hermes.md` (the operator's tools). Run it with `tradingagents desk`.

## Why

Hermes on Telegram is the operator's phone: short answers, and a yes or no to
an action. Two jobs don't fit a chat:

- **Adopting a lab variant.** It's a choice among dozens of variants on ten
  figures each, against a verdict that often says "no change". That needs a
  sortable table, and the evidence should be recorded with the choice.
- **Seeing the paper book at a glance.** The switches, the pending plan's
  orders, the cohorts and the account, all on one screen.

Desk is a local page for those jobs. It is not a second system: every panel
reads the same files, and every button calls the same functions, as the CLI and
the Hermes tools.

## Shape

| Tab | Shows | Actions |
|---|---|---|
| Overview | The morning report, open lab suggestions, and jobs (the MCP tools' own text) | -- |
| Lab | Per strategy: what the live screen uses; whether that screen reads adoptions; the verdict; every variant, sortable, with the shorter-hold read where there is one; the adoption history; the full report | **Adopt** (the reason is required) |
| Trading | Halted / blocked / allowed; the account; the pending plan's orders; live cohorts; recent plans; events | Approve and send, halt, resume, acknowledge, reconcile |
| Decisions | `list_decisions` with filters; a decision's full report | -- |

- **Server:** Starlette on uvicorn (the optional `desk` extra; both are already
  installed with `mcp`).
- **Page:** one HTML page with plain JS and CSS. No build step and no Node.
- **Code:** `tradingagents/desk/views.py` returns plain data and is tested
  without HTTP. `app.py` is the thin HTTP layer.

## Adoption, recorded

`lab run` now also writes `<strategy>-results.json`: every variant's tuning and
test figures, and the verdict. An adoption, from Desk or from `lab adopt`,
appends to `adopted.jsonl`:

- the variant and the reason;
- the variant's figures from the last run;
- that run's verdict;
- `lab_candidate`: whether this was the lab's passing pick or the operator's
  call against it.

Desk shows the difference before you confirm. Adopting against the verdict is
allowed, because the bar is deliberately conservative, but it is labelled as
your call, and it needs a reason.

**All three live screens read their adoption.** Standard takes the signal and
floor. Momentum and value take the whole variant: the liquidity budget
(`/topN`, so only budgeted variants can be adopted for them), the floor, the
ordering (value measures through `lab.fundamentals`, price signals through
`lab.live`, the lab's own code), the pick count, and for value the quality
switch (`/noq` turns the quality exclusions off; a name that cannot be judged
at all is still excluded). With nothing adopted, a screen runs exactly as
before. The manifest's ordering line and notes say which adoption was used.

## Guards

This page can send orders, so:

- **Loopback only.** It listens on 127.0.0.1 and answers only `Host:
  127.0.0.1` or `localhost`, which defeats DNS rebinding.
- **A token on every action.** Every action is a POST carrying `X-Desk-Token`,
  a secret made at start-up and embedded in the page the server serves:
  - Another site in the same browser can't read the page, so it can't learn
    the token.
  - It can't send the header without a CORS preflight, which is never granted.
  - A foreign `Origin` is refused as well.
- **The same refusals as the terminal.** Halt, reconciliation block, the 09:28
  expiry, and paper-only unless `trading_live` all come from `trading.execute`.
  A plan id must be a plain name, not a path.
- **Resume and acknowledge are allowed here but not in Hermes.** Desk runs on
  the Mac itself, like the terminal. Hermes is remote.
- **Halt never needs the broker**, as at the terminal.

## Next

1. Lab runs from Desk: start `lab run` as a detached job through `ops.jobs`,
   with the same night-window and busy guards.
2. Charts where they earn their place: the ladder per strategy over time, and
   cohort P&L against SPY.
3. Build a plan from Desk (`trade plan`); for now the nightly run builds it.
