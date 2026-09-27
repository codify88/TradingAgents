# Desk: the operator's local web UI

Status: v1 (the local page) built 2026-09-25; the app -- phases D0 and D1 of
the approved proposal (https://claude.ai/artifact/Y1CsN7MykYD48GMRN6M2EV) --
built 2026-09-26. Builds on `go-live.md` (the lab, paper trading) and
`hermes.md` (the operator's tools). Runs as a LaunchAgent on
http://localhost:8810 (`tradingagents desk` by hand). Port 8810, not 8765:
brooks-bot already serves on 8765.

## The app (D0 + D1)

| Screen | Shows | Actions |
|---|---|---|
| Today (`/`) | An inbox, most urgent first: halts, mismatches, a failed night, the pending order plan with its cutoff countdown, reviews due, lab suggestions | Hold to approve the plan (then the passkey); skip; ⌘↵ on a keyboard |
| Plan (`/plan/:id`) | Every order and its reason, sessions, equity, carried names, the planner's notes | -- |
| Book (`/book`) | Halted / blocked / allowed, the account, live cohorts, recent events | Resume, acknowledge (passkey); reconcile |
| Security (`/security`) | This address's passkeys | Enrol this device with a one-time code |
| Agents (`/agents`) | The agent lab (`docs/design/agent-lab.md`) | Variants, suites, runs |
| Research (`/research?s=SYM`) | Any company, ETF or index: every data tool's output grouped (price and technicals, statements, the latest earnings call, news, insiders / Congress / 13F / ETFs, macro, each mandate's metrics), agent research runs, and the book's own decisions on it | Fetch all data (token; vendor requests only); run the agents with a mandate and models (token and the confirmed estimate); both |
| Lab | The v1 page (`/classic#lab`) until D3 | as v1 |

Halt is in the header on the phone and at the foot of the sidebar on a desktop,
on every screen, one tap and a one-line confirm, never a passkey.

**API.** `/api/v1/today` (``desk/today.py``: one read model for the first
screen), `/api/v1/trading`, `/api/v1/session` (the action token, and whether
this address has a passkey), `/api/v1/passkeys/...`, `/api/v1/trade/{action}`.
The v1 page's `/api/...` routes stay as they were.

**Stack, as built** (every package at its release current on 2026-09-26, pinned
in `desk-ui/package-lock.json`): FastAPI 0.141, uvicorn 0.54, webauthn 3.0;
React 19.3, TypeScript 7.0, Vite 8.3, TanStack Router 1.170 and Query 5.103,
radix-ui 1.6, Tailwind 4.3, lucide-react 1.48, @simplewebauthn/browser 14.0,
tailwind-merge 3.7, fonts self-hosted from @fontsource 5.3; Vitest 5.0 and
Playwright 1.63. Where it departs from the proposal, and why:

- **npm, not pnpm**: npm 11 is already on the Mac; the lockfile does the same job.
- **No shadcn CLI, clsx or class-variance-authority**: shadcn's components
  pull in clsx (last release April 2024) and cva (November 2024), older than
  the twelve-month rule. The few components Desk needs are written in its
  style on radix-ui, with a two-line `cn` on tailwind-merge.
- **No vite-plugin-pwa**: its workbox build pulls a deprecated `glob`. The
  manifest and a forty-line service worker (`public/sw.js`) are written by
  hand; the worker caches only hashed assets, never a page or the API, so a
  plan or a halt is never shown from a cache.
- **No web push yet**: D3, with the suggestions.

**Build and test.** `cd desk-ui && npm run build` (the server serves
`desk-ui/dist`; without it, `/` falls back to the v1 page). `npm test` (unit),
`npm run e2e` (Playwright on a phone and a desktop viewport, against the real
API with the fake broker, and Chromium's virtual passkey authenticator: enrol,
hold-to-approve, halt, resume, a spent enrolment code).

## Research

`desk/research.py` and `desk/research_api.py`. **Fetch** calls every tool the
agents and the mandates can call for one symbol and date, six at a time, in a
server thread; it saves `<results_dir>/research/<SYM>/data-<date>.json` as
sections finish, so the screen fills in live. Each section is `ok`, `empty`
(the source has nothing), `unavailable` (no key, vendor or data, e.g. FRED
without `FRED_API_KEY`) or `error`. The ownership and transcript tools are given
the as-of date explicitly (in the graph they read it from state). An index
(`^GSPC`, `^DJI`, `^IXIC`, `^RUT`, `^VIX`) skips company-only tools.

**Run** is the full pipeline on one name, with or without a mandate and with any
priced model, as a background job (`tradingagents research execute`). It builds
its own graph with its own results dir and memory log and no checkpoint, so it is
never logged as a decision, never feeds reflection and never trades. Jobs are
refused in the night window but not by other jobs, since a research run touches
nothing they write. The cost is one decision at the agent lab's per-decision
figures (about $0.36 on Haiku, $1.80 on the Opus 4.8 / Sonnet 5 defaults), and
the start must carry that confirmed estimate. CLI: `tradingagents research fetch
SYM --date D`, `tradingagents research run SYM --date D --mandate M [--deep X --quick Y]`.

Symbol search reads the stored Alpha Vantage `LISTING_STATUS` (no request).
Tool output renders as markdown with react-markdown and remark-gfm, which never
render raw HTML; CSV output becomes a table, newest row first.

## Passkeys

Money actions -- approve a plan, resume, acknowledge -- each need a WebAuthn
assertion with user verification (Face ID, Touch ID) over a challenge made for
that action and that plan, used once, within two minutes (`desk/passkeys.py`).
The token alone gets a 401. Enrolling a device needs a one-time code from the
Mac's terminal (`tradingagents desk code`, fifteen minutes, one use), so
reaching Desk is not enough to enrol. A passkey belongs to the host name it was
made on: enrol once at `localhost` on the Mac and once at the Tailscale name
from the phone. Credentials (public keys, sign counters) are in
`<trading_dir>/desk/passkeys.json`, mode 600.

The v1 page's approve, resume and acknowledge work only on the Mac itself
(loopback Host, no proxy headers), as the terminal does; from anywhere else
they answer "use the app, with your passkey".

## From the phone: Tailscale

Desk listens on 127.0.0.1 only. The phone reaches it through `tailscale
serve`, which gives the Mac a private HTTPS name on your tailnet (HTTPS is
required for passkeys and for installing the app):

1. Install Tailscale on the Mac and the phone, and sign in to the same account
   on both. In the admin console, turn on MagicDNS and HTTPS certificates.
2. On the Mac: `tailscale serve --bg 8810`. It prints the address, e.g.
   `https://your-mac.tailnet-name.ts.net`.
3. Tell Desk its name: `TRADINGAGENTS_DESK_HOSTS=your-mac.tailnet-name.ts.net`
   in the repo's `.env`, then restart the agent
   (`launchctl kickstart -k gui/$(id -u)/com.jeremysmith.tradingagents.desk`).
4. On the phone, open that address in Safari, Share -> Add to Home Screen.
   In Security, enter a code from `tradingagents desk code` and create the
   passkey.

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

1. D2: Strategies and Book (the go-live gate, the ladder waterfall per
   strategy, the paper record against SPY and the style index).
2. D3: the Lab in the app, adoption with the evidence beside it, and web push
   when a variant clears the bar or a cutoff is missed.
3. D5: System and the command palette. (D4, Research, is built: see
   "Research" below.)
4. Lab runs from Desk as detached jobs (`ops.jobs`); building a plan from Desk.
