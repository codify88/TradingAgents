# TradingAgents CLI guide

Everything the `tradingagents` command can do on this fork, with the workflows
that tie the pieces together. Examples are real invocations; where output is
shown it is trimmed from an actual run.

- [The mental model](#the-mental-model)
- [Setup](#setup)
- [`tradingagents` — analyze one name](#tradingagents--analyze-one-name)
- [Mandates](#mandates)
- [The decision log](#the-decision-log)
- [`screen` — find candidates without an LLM](#screen--find-candidates-without-an-llm)
- [`screen-run` — adjudicate a screen](#screen-run--adjudicate-a-screen)
- [`backtest` — score decisions over a grid](#backtest--score-decisions-over-a-grid)
- [`screen-review` — does the screen earn its keep?](#screen-review--does-the-screen-earn-its-keep)
- [Recipes](#recipes)
- [Using it from Python](#using-it-from-python)
- [Cost and time](#cost-and-time)
- [Caveats and troubleshooting](#caveats-and-troubleshooting)

---

## The mental model

Five commands, one loop:

```
  screen ──▶ shortlist + random control ──▶ screen-run ──▶ decision log(s) ──▶ screen-review
   (no LLM)        (manifest on disk)       (LLM loop)       (graded later)       (picks vs control)

  backtest: the same LLM loop over any ticker/date grid you choose

  tradingagents (interactive) ──▶ one decision ──▶ live decision log ──▶ graded on a later run
```

- **A run** is ~30 LLM calls across analysts, a bull/bear debate, a trader, a
  three-way risk debate and a portfolio manager. It produces one rating:
  Buy / Overweight / Hold / Underweight / Sell.
- **A mandate** decides *whose* decision it is: the extra analysts that run, the
  framing every agent reads, what "risk" means, and the horizon the decision is
  graded over. No mandate reproduces upstream behaviour exactly.
- **The decision log** records every rating and, once its horizon has traded,
  the outcome and a reflection that later runs read back.
- **The screener** decides what is worth paying a run for, and carries a random
  control so you can tell whether its ordering beats chance.

Where things live (all under `~/.tradingagents/` unless overridden):

| Path | What |
|---|---|
| `memory/trading_memory.md` | the live decision log (`TRADINGAGENTS_MEMORY_LOG_PATH`) |
| `logs/<TICKER>/<date>/` | per-run working files from the interactive CLI |
| `logs/reports/<TICKER>_<stamp>/` | saved report tree: one `.md` per persona + `complete_report.md` |
| `logs/backtest/<run_id>/` | one sweep: its own `trading_memory.md`, plus `<TICKER>/TradingAgentsStrategy_logs/full_states_log_<date>.json` per cell |
| `logs/screens/*.json` | one manifest per screen run |
| `cache/` | price and data cache, checkpoints (`TRADINGAGENTS_CACHE_DIR`) |
| `cli_prefs.json` | your last interactive selections, offered back as defaults |

`logs/` is `results_dir` and moves with `TRADINGAGENTS_RESULTS_DIR`.

---

## Setup

Keys go in `.env` at the repo root (it is gitignored). The package loads it on
import, so the CLI, the Python API and scripts all see the same values.

```bash
ANTHROPIC_API_KEY=...           # or the key for whichever provider you use
ALPHA_VANTAGE_API_KEY=...       # required by the mandate tools and the screener
FRED_API_KEY=...                # optional: macro data
```

The mandate analysts' tools need **Alpha Vantage** specifically: yfinance
carries about four years of statements, too few to judge a decade of returns
on capital. Without the key those tools return an explicit `UNAVAILABLE` and
the analysts are told to report the gap rather than fill it from memory.

### Settings through the environment

Every interactive choice below can be pinned with an environment variable; the
CLI skips the matching prompt. Put them in `.env` for a standing default, or on
the command line for one run.

| Variable | Sets | Example |
|---|---|---|
| `TRADINGAGENTS_MANDATE` | investment mandate | `equity_value`, `equity_momentum`, empty for none |
| `TRADINGAGENTS_LLM_PROVIDER` | provider | `anthropic`, `openai`, `google`, `deepseek`, … |
| `TRADINGAGENTS_DEEP_THINK_LLM` | model for research manager + PM | `claude-opus-4-8` |
| `TRADINGAGENTS_QUICK_THINK_LLM` | model for everything else | `claude-sonnet-5` |
| `TRADINGAGENTS_ANTHROPIC_EFFORT` | Claude effort | `low`, `medium`, `high` |
| `TRADINGAGENTS_OPENAI_REASONING_EFFORT` | OpenAI reasoning effort | `low`, `medium`, `high` |
| `TRADINGAGENTS_GOOGLE_THINKING_LEVEL` | Gemini thinking | `minimal`, `high` |
| `TRADINGAGENTS_MAX_DEBATE_ROUNDS` | bull/bear rounds | `1` (default) |
| `TRADINGAGENTS_MAX_RISK_ROUNDS` | risk-debate rounds | `1` (default) |
| `TRADINGAGENTS_OUTPUT_LANGUAGE` | report language | `English` (debate stays English) |
| `TRADINGAGENTS_CHECKPOINT_ENABLED` | resume after a crash | `true` |
| `TRADINGAGENTS_BENCHMARK_TICKER` | force one alpha benchmark | `SPY` |
| `TRADINGAGENTS_TEMPERATURE` | sampling temperature | `0.2` |
| `TRADINGAGENTS_MAX_TOKENS` | output-token cap | `8000` |
| `TRADINGAGENTS_LLM_MAX_RETRIES` | SDK retry budget | `6` for bursty 429s |
| `TRADINGAGENTS_LLM_BACKEND_URL` | custom endpoint | an OpenAI-compatible gateway |
| `TRADINGAGENTS_RESULTS_DIR` | `results_dir` | `/data/ta/logs` |
| `TRADINGAGENTS_CACHE_DIR` | `data_cache_dir` | `/data/ta/cache` |
| `TRADINGAGENTS_MEMORY_LOG_PATH` | live decision log | `/data/ta/memory.md` |

---

## `tradingagents` — analyze one name

```bash
tradingagents
```

The interactive flow, and what can skip each step:

| Step | Asks for | Skipped by |
|---|---|---|
| 1 | ticker (crypto is auto-detected from the symbol) | — |
| 2 | **investment mandate** | `TRADINGAGENTS_MANDATE` |
| 3 | analysis date | — |
| 4 | output language | `TRADINGAGENTS_OUTPUT_LANGUAGE` |
| 5 | analysts: market, social (sentiment), news, fundamentals | — (last choice offered as default) |
| 6 | research depth: Shallow 1 / Medium 3 / Deep 5 rounds | both `…_MAX_DEBATE_ROUNDS` and `…_MAX_RISK_ROUNDS` |
| 7 | LLM provider | `TRADINGAGENTS_LLM_PROVIDER` |
| 8 | quick and deep models | either model variable |
| 9 | provider effort / thinking level | the matching effort variable |

Your previous run's answers are offered as defaults (`~/.tradingagents/cli_prefs.json`);
the ticker never is.

With a mandate, the mandate's own analysts run **after** the ones you select and
before the debate — you do not select them. They appear in the live status
table as their own rows and in the saved report as their own files.

### Flags

```bash
tradingagents --portfolio book.json      # size against your actual position
tradingagents --supersede                # replace today's logged decision for this name+mandate
tradingagents --checkpoint               # save state after every node; resume a crashed run
tradingagents --no-checkpoint            # override TRADINGAGENTS_CHECKPOINT_ENABLED=true
tradingagents --clear-checkpoints        # delete saved checkpoints, force a fresh start
```

### `--portfolio`: tell the agents what you already hold

Without it, the trader and risk agents cannot tell "add to a full position"
from "open a new one". The file:

```json
{
  "cash": 25000,
  "currency": "USD",
  "positions": [
    {"ticker": "KO",   "quantity": 400, "average_price": 61.20},
    {"ticker": "PEP",  "quantity": 150, "average_price": 168.00},
    {"ticker": "TSLA", "quantity": -20}
  ]
}
```

Quantities are signed units (negative is short); `average_price` is optional.
The analyzed name is shown first, then cash, then everything else held. Three
states are kept distinct on purpose: a position, a flat book (`"positions": []`),
and no `--portfolio` at all — "not provided" is never treated as "flat".

A changed portfolio file changes the checkpoint key, so a resumed run cannot
silently continue against a stale book.

### `--supersede`: when a re-run should replace the record

A second run for the same ticker, date and mandate is normally **not logged**:
an incidental re-run must not count the same decision twice. That is wrong when
you re-ran *because the analysis improved* — new tools, a fixed data problem, a
better model. `--supersede` marks the old entry `superseded:<timestamp>` (kept,
not deleted) and records the new one, which is the one later runs read and the
one that gets graded.

```bash
# The value tools changed since this morning's KO run; replace its decision.
TRADINGAGENTS_MANDATE=equity_value tradingagents --supersede
```

### Checkpoints

```bash
TRADINGAGENTS_CHECKPOINT_ENABLED=true tradingagents   # crash at the risk debate?
TRADINGAGENTS_CHECKPOINT_ENABLED=true tradingagents   # same ticker+date: resumes there
```

A checkpoint is keyed on everything that shapes the graph — analysts, debate and
risk depth, asset type, mandate, the mandate's own analysts, and the portfolio.
Change any of them and the run starts fresh rather than resuming into a
different graph. A completed run clears its own checkpoint.

---

## Mandates

| | `equity_value` | `equity_momentum` | none |
|---|---|---|---|
| Question | durable business at a sensible price? | real, accelerating growth the trend confirms? | upstream's short-horizon trade |
| Graded over | 504 trading days (~2 years) | 126 trading days (~6 months) | 5 trading days |
| Interim reviews | 63, 126, 252 days | 21, 63 days | — |
| Extra analysts | Quality, Valuation | Momentum, Growth | — |
| Risk means | permanent loss of capital | the trend breaking while you are in it | upstream framing |
| Exits on | a named thesis break, not a price stop | a named invalidation level, measured in ATRs | — |
| Indicators offered | 50/200 SMA, ATR | 8, trend and momentum | all 12 |

**Value analysts' tools** (all computed, point-in-time):
`get_quality_metrics` (10 years of ROIC, return on tangible capital, margins, FCF
conversion, the last eight quarters of cash flow), `get_capital_allocation`
(where a decade of operating cash went), `get_valuation_history` (EV/EBIT, P/E,
FCF yield against the company's own ten-year range), `get_reverse_dcf` (the
growth the price implies, across a 7/8/9% discount-rate grid).

**Momentum analysts' tools:** `get_relative_strength`, `get_trend_structure`,
`get_growth_trajectory`, `get_estimate_revisions`. The two momentum analysts
cannot see each other's tools on purpose: price and fundamental momentum
disagreeing is itself one of the mandate's screens, and it should reach the
debate rather than be reconciled away first.

**Screens** in the tool output report `TRIPPED`, `WATCH`, `CLEAR` or `NO DATA`,
always with the threshold beside the measured value. `WATCH` means the numbers
meet a disqualifier on one reading and not another — the analyst must say which
reading holds and why. Where a verdict depends on an assumption, the screen uses
the one most generous to the stock, so `TRIPPED` never rests on a contestable
input.

Choosing: run a name under **both** when you want to know whether it is a
business to own or a trend to ride — they are separate log entries, graded on
separate clocks (see [recipe 3](#3-the-same-name-under-both-mandates)).

---

## The decision log

`~/.tradingagents/memory/trading_memory.md` is plain markdown; read it, grep it,
diff it. One entry per decision:

```
[2026-09-17 | KO | Hold | pending | mandate:equity_value]

DECISION:
**Rating**: Hold
...

REVIEW 63d @ 2026-12-16: raw +3.2% | alpha -1.1%
Tracking but lagging SPY by 1.1pp; at 13% of the horizon that is noise.
```

and once its horizon has traded:

```
[2026-09-17 | KO | Hold | +21.0% | +4.0% | 504d | resolved:2028-09-15 | mandate:equity_value]
...
REFLECTION:
...
```

- **Grading happens at the start of the next run for that ticker**, not on a
  timer. An entry for a name you never re-run stays pending. (`backtest` settles
  every ticker in its grid at the end of the sweep.)
- **Interim reviews** are checkpoints, not verdicts: they say whether the thesis
  is tracking, never whether it was right. Later runs see them labelled
  `in progress`.
- **Point-in-time**: a historical run only reads lessons whose outcome was known
  by its analysis date, and checkpoints are held to the same rule.

```bash
LOG=~/.tradingagents/memory/trading_memory.md
grep '^\[' "$LOG"                           # every decision, one line each
grep '^\[.*| pending' "$LOG"                # everything still open
grep -A2 '^REVIEW' "$LOG"                   # interim checkpoints
grep '^\[.*mandate:equity_value' "$LOG"     # one mandate's record
grep 'superseded:' "$LOG"                   # decisions replaced by a re-run
```

---

## `screen` — find candidates without an LLM

```bash
tradingagents screen --mandate equity_value
```

The funnel (as run against the full US universe):

| Tier | Narrows | Costs |
|---|---|---|
| universe | ~14,400 listings → ~5,700 common stocks | one `LISTING_STATUS` call (two for a past date) |
| price | ~6,300 → ~230 | one batched download per 200 names |
| liquidity budget | ~230 → `--budget` (60) | nothing — a neutral cut |
| fundamentals | 60 → ~30 | a few Alpha Vantage calls per name |
| ordering | ~30 → `--picks` + `--controls` | nothing |

The universe tier keeps common shares only. About one Alpha Vantage "Stock"
row in seven is really a warrant, right, unit, note or preferred; those are
dropped by NASDAQ's symbol convention (a listed symbol plus a reserved suffix:
ZION+O, AGNC+N) or by the filed name ("- Warrants (30/06/2028)"). Class
shares such as GOOGL and FOXA are kept.

Two rules make it honest:

1. **Exclusions narrow; the thesis does not.** Names are removed only where a
   mandate's own disqualifier comes back `TRIPPED`. `WATCH` and `NO DATA` never
   exclude. `equity_value` has no price-based exclusion at all — a cheap stock in
   a downtrend is the point.
2. **Every screen carries a random control** drawn from eligible names the
   ordering did not pick. It is the only way to answer whether the ordering
   beats picking an eligible name at random.

The ordering is one declared number per mandate, printed in the report:
`equity_value` → FCF yield percentile against the company's own ten-year
history; `equity_momentum` → 12-month excess total return over SPY.

| Flag | Default | Use |
|---|---|---|
| `--mandate` | required | `equity_value` or `equity_momentum` |
| `--date` | today | as-of date (historical listings back to 2010; see [survivorship](#caveats-and-troubleshooting)) |
| `--picks` | 8 | shortlist size |
| `--controls` | 3 | random controls; raise it for a sharper comparison |
| `--budget` | 60 | how many price-tier survivors get fundamentals calls |
| `--universe-limit` | none | alphabetical cap for a quick end-to-end check |
| `--seed` | fresh | reproduce a control draw exactly |
| `--show-excluded` | off | list every excluded name and its reason |

```bash
# A two-minute smoke test: first 60 symbols alphabetically, reproducible control.
tradingagents screen --mandate equity_momentum --universe-limit 60 --picks 3 --controls 2 --seed 7
```

```
fundamental
 • 5 — Not actually a growth business
 • 2 — Growth decelerating while the multiple is still expanding

Shortlist — Ordered by: 12-month excess total return over SPY
 Rank  Symbol  Ordering value
 1     AAMI    +0.717
 2     AAUC    +0.352
 3     AAPL    +0.243

Control group — drawn at random (seed 7) from the 6 names that cleared every exclusion
 ABBV    +0.064
 A       +0.076

Manifest: ~/.tradingagents/logs/screens/2026-09-18_equity_momentum_165135.json
Run the shortlist and its control through the loop:
  tradingagents backtest AAMI,AAUC,AAPL,ABBV,A --start 2026-09-18 --end 2026-09-18 --mandate equity_momentum
```

Add `--run` to adjudicate the shortlist and control straight away, or run it
later with the `screen-run` command the screen prints.

Budget trade-off: `--budget` is the only knob that spends Alpha Vantage calls.
Raising it examines more names on fundamentals; it does not change what counts
as eligible, only how many liquid names get a look.

---

## `screen-run` — adjudicate a screen

`screen-run` takes a saved screen's picks and control through the agent loop together, as one sweep dated at the screen's
as-of date and run under its mandate.

```bash
tradingagents screen-run 165135                # the short id screen and screen-review print
tradingagents screen-run latest --analysts market,fundamentals
tradingagents screen-run --all --dry-run       # what is unfinished, and how long it would take
tradingagents screen-run --all                 # run everything unfinished, oldest first
tradingagents screen --mandate equity_value --run   # screen, then adjudicate, in one go
```

| Flag | Use |
|---|---|
| `SCREEN_ID` | full id, short id (the time suffix), or `latest` |
| `--all` | every saved screen with undecided names |
| `--mandate` | with `--all`: only this mandate's screens |
| `--analysts` | which upstream analysts run; the mandate's own always do |
| `--dry-run` | list names and an estimate (~8 min each), spend nothing |

- **Resumable.** The sweep's id comes from the screen's (`scr_20220301_momentum_165135`), and a name counts as done only if
  it was decided on the screen's date under its mandate. An interrupted run continues by running it again, and `--all` is
  safe to schedule.
- **Scored automatically.** Outcomes land in the sweep's log, which `screen-review` reads.
- **Delisted names can still be priced.** For a historical screen, Alpha Vantage is added after your configured price
  vendors, so a company Yahoo has dropped still gets prices, indicators and a verified snapshot (quoted as reported).
  Live screens run on your vendors unchanged.

---

## `backtest` — score decisions over a grid

```bash
tradingagents backtest KO,PEP,MDLZ --start 2023-01-02 --end 2023-12-25 --every 28 --mandate equity_value
```

Every ticker is run on every date in the grid (`--start` to `--end`, every
`--every` days, never past today), then each ticker is settled.

| Flag | Default | Use |
|---|---|---|
| `TICKERS` | required | comma-separated |
| `--start` / `--end` | required | grid bounds, `YYYY-MM-DD` |
| `--every` | 7 | days between analysis dates |
| `--mandate` | `TRADINGAGENTS_MANDATE` | mandate for every cell; `--mandate ""` forces none |
| `--analysts` | all four | e.g. `market,fundamentals` |
| `--asset-type` | stock | or `crypto` |
| `--portfolio` | none | one standing book for every cell (not carried forward) |
| `--run-id` | timestamp | continue an earlier sweep |

It prints the mandate it resolved before starting — check it; a value sweep run
without one grades on five days and never sees the Quality or Valuation analysts.

**It never touches the live log.** Each sweep writes its own
`logs/backtest/<run_id>/trading_memory.md`, so a hundred historical decisions
cannot flood the context your real runs read back.

**Resume by re-running with the same `--run-id`.** A cell is
(ticker, date, mandate): cells already in the sweep's log are skipped, and a
sweep continued under a *different* mandate re-runs its cells rather than
treating them as done.

```bash
# Crashed or rate-limited halfway? Same command, same run id.
tradingagents backtest KO,PEP,MDLZ --start 2023-01-02 --end 2023-12-25 --every 28 \
  --mandate equity_value --run-id value_staples_2023
```

**The horizon decides what can be scored.** A cell settles only once its
mandate's horizon has fully traded:

| Mandate | A cell settles after | Latest date that can settle today (2026-09-18) |
|---|---|---|
| none | 5 trading days | ~2026-09-10 |
| `equity_momentum` | 126 trading days | ~2026-03-17 |
| `equity_value` | 504 trading days | ~2024-09-16 |

Cells newer than that stay pending; the summary counts them separately rather
than averaging them in. Interim reviews still land in the sweep's log, so a
recent value sweep is not silent — read the `REVIEW` blocks.

**Reading the summary.** Scored per rating: count, mean alpha, and a hit rate
for directional ratings. A Sell is right when the name fell against its
benchmark. **Hold claims no direction, so it gets no hit rate** — only its mean
alpha. Decisions with no readable rating are reported as unscored.

**Is conviction earning its keep?** Below the per-rating lines, a *Conviction*
section asks whether stronger ratings produced bigger moves in their direction
-- the only reason to have five ratings rather than three:

- **Tilted alpha**: the ratings read as position sizes (full for Buy and Sell,
  half for Overweight and Underweight, none for Hold), averaged over settled cells.
- **Rank correlation** between conviction and alpha: positive means stronger
  calls did better in the direction they claimed.
- **Order**: whether Buy beat Overweight beat Hold, and so on to Sell. An
  inversion is named ("Overweight beat Buy"). Ratings with fewer than 5 settled
  cells are listed as too thin and left out of the ordering.

It evaluates **decision quality**, not a portfolio: there are no fills, sizes or
cash ledger, and every cell is independent.

---

## `screen-review` — does the screen earn its keep?

```bash
tradingagents screen-review                       # every screen
tradingagents screen-review --mandate equity_value
```

Joins every saved manifest to its outcomes and compares picks with their
control:

```
| Screen | Style    | As of      | Picks | Picks α | Control | Control α | Edge  |
| 165135 | momentum | 2026-09-18 | 3/3   | +5.0%   | 2/2     | -1.0%     | +6.0% |
```

- Outcomes are read from the live log **and every backtest sweep's log**, so
  running the command `screen` prints is enough to feed this report. Where a name
  ran more than once, the most recent sweep's outcome wins.
- Only decisions made **under the screen's mandate** on the screen's as-of date
  count. The same name analysed that day under another mandate is a different
  decision and is ignored.
- It **refuses a verdict until both arms settle.** Picks returning well while no
  control has settled is a statement about the market, not about the screen.
- The **Edge** column is the one that matters: beating the market while losing
  to your own control means the exclusions work and the ordering does not.

---

## Recipes

### 1. A monthly value cycle

```bash
# 1. Screen. Fix the seed so the control draw is reproducible later.
tradingagents screen --mandate equity_value --picks 8 --controls 4 --seed 2026

# 2. Adjudicate picks + controls (one resumable sweep, mandate and date carried).
tradingagents screen-run latest --analysts market,fundamentals

# 3. Read the analysis. A sweep keeps each cell's full state as JSON (every
#    persona's report, the debates, the decision) rather than a report tree:
ls ~/.tradingagents/logs/backtest/scr_*_value_*/*/TradingAgentsStrategy_logs/
#    To get the usual one-file-per-persona tree for a cell:
#    (the state log names the trader's plan differently and omits the mandate,
#    so both are supplied here)
.venv/bin/python -c "import json,sys; from tradingagents.reporting import write_report_tree as w; \
s=json.load(open(sys.argv[1])); s['mandate']='equity_value'; \
s['trader_investment_plan']=s.get('trader_investment_decision',''); print(w(s, 'KO', 'ko_report'))" \
  ~/.tradingagents/logs/backtest/scr_20260918_value_<id>/KO/TradingAgentsStrategy_logs/full_states_log_2026-09-18.json

# 4. Months later: interim reviews appear in the sweep's log as the 63/126/252-day
#    marks pass. Two years later, screen-review can score it.
tradingagents screen-review --mandate equity_value
```

### 2. Validate the screener on history before trusting it

A live screen needs two years to score under `equity_value`. Screen the past
instead, and let the backtest settle it immediately:

```bash
for d in 2022-03-01 2022-09-01 2023-03-01 2023-09-01; do
  tradingagents screen --mandate equity_momentum --date "$d" --picks 5 --controls 5 --seed 1
done
tradingagents screen-run --all --mandate equity_momentum --dry-run   # 40 names, ~5 hours
tradingagents screen-run --all --mandate equity_momentum
tradingagents screen-review --mandate equity_momentum
```

Momentum settles after six months, so every date above is scorable today. Read
the **Survivorship** line each screen prints: it counts the delisted names the
fundamentals tier could not examine, which is the bias that remains.

### 3. The same name under both mandates

```bash
TRADINGAGENTS_MANDATE=equity_value    tradingagents     # KO, 2026-09-17
TRADINGAGENTS_MANDATE=equity_momentum tradingagents     # KO, 2026-09-17
grep '^\[2026-09-17 | KO' ~/.tradingagents/memory/trading_memory.md
```

Two entries, two clocks: the value call grades in two years, the momentum call
in six months. On 2026-09-17 both said Hold on KO for different reasons — the
value run on no margin of safety (price implies ~9.4% FCF growth against a 4.7%
record), the momentum run on failed acceleration.

### 4. Cheap while iterating, expensive when it counts

```bash
# Iterating on prompts or tools: fast models, shallowest depth, two analysts.
TRADINGAGENTS_DEEP_THINK_LLM=claude-sonnet-5 TRADINGAGENTS_QUICK_THINK_LLM=claude-haiku-4-5 \
TRADINGAGENTS_MAX_DEBATE_ROUNDS=1 TRADINGAGENTS_MAX_RISK_ROUNDS=1 \
  tradingagents backtest KO --start 2024-06-03 --end 2024-06-03 --analysts market,fundamentals \
  --mandate equity_value --run-id scratch

# The run that goes in the record.
TRADINGAGENTS_DEEP_THINK_LLM=claude-opus-4-8 TRADINGAGENTS_ANTHROPIC_EFFORT=high \
TRADINGAGENTS_MAX_DEBATE_ROUNDS=3 TRADINGAGENTS_MAX_RISK_ROUNDS=3 \
  TRADINGAGENTS_MANDATE=equity_value tradingagents --portfolio ~/book.json
```

The mandate analysts always run on the quick model; the research manager and
portfolio manager use the deep one.

### 5. Unattended runs

Everything except ticker and date can come from the environment, and a backtest
of one date is a non-interactive single run:

```bash
#!/usr/bin/env bash
# weekly.sh — re-run the watchlist under the value mandate, one sweep per week.
set -euo pipefail
cd ~/GIT/trade-agents
TODAY=$(date +%F)
.venv/bin/tradingagents backtest KO,PEP,JNJ,PG \
  --start "$TODAY" --end "$TODAY" --mandate equity_value \
  --portfolio ~/book.json --run-id "weekly_$TODAY"
```

Note that a sweep writes to its own log, so these decisions will not appear in
the live log your interactive runs read back. That is deliberate; if you want
them there, run the interactive CLI instead.

The screener runs unattended the same way, as a screen followed by its adjudication.
`screen-run --all` resumes anything a previous night left unfinished:

```bash
#!/usr/bin/env bash
# monthly-screen.sh — first of the month: screen, then adjudicate.
set -euo pipefail
cd ~/GIT/trade-agents
.venv/bin/tradingagents screen --mandate equity_momentum --picks 5 --controls 5
.venv/bin/tradingagents screen-run --all --analysts market,fundamentals
```

### 6. Re-grade after improving the analysis

```bash
# The tools improved; replace today's decision instead of silently skipping the re-run.
TRADINGAGENTS_MANDATE=equity_value tradingagents --supersede
grep 'superseded:' ~/.tradingagents/memory/trading_memory.md
```

### 7. A disposable sandbox

Point every path somewhere temporary to try something without touching your
real log, screens or reports:

```bash
export TRADINGAGENTS_RESULTS_DIR=/tmp/ta TRADINGAGENTS_MEMORY_LOG_PATH=/tmp/ta/memory.md
tradingagents screen --mandate equity_value --universe-limit 100
tradingagents screen-review
```

---

## Using it from Python

The CLI is a thin layer; everything is callable directly.

```python
from dotenv import load_dotenv; load_dotenv()
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.portfolio import load_portfolio

config = {**DEFAULT_CONFIG, "max_debate_rounds": 2}
graph = TradingAgentsGraph(["market", "fundamentals"], config=config, mandate="equity_value")
state, rating = graph.propagate("KO", "2026-09-17", portfolio=load_portfolio("book.json"))

print(rating)                                   # "Hold"
print(state["mandate_reports"]["valuation"])    # the Valuation Analyst's report
graph.save_reports(state, "KO")                 # same report tree the CLI writes
```

```python
from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.agents.utils.memory import TradingMemoryLog

result = run_backtest(["KO", "PEP"], iter_grid("2023-01-02", "2023-06-26", 28),
                      DEFAULT_CONFIG, mandate="equity_momentum",
                      selected_analysts=["market", "fundamentals"], run_id="py_sweep")
print(summarize(TradingMemoryLog({"memory_log_path": str(result.log_path)})).render())
```

```python
from tradingagents.screener import run_screen, save_manifest

result = run_screen("equity_value", "2026-09-18", DEFAULT_CONFIG, picks=5, controls=5, control_seed=1)
print(result.manifest.pick_symbols, result.manifest.control_symbols)
save_manifest(result.manifest, DEFAULT_CONFIG)
```

The value tools are plain functions too — useful for checking a number an
analyst cited:

```python
from tradingagents.mandates.tools.value_tools import get_reverse_dcf
print(get_reverse_dcf.invoke({"ticker": "KO", "curr_date": "2026-09-17"}))
```

---

## Cost and time

| Operation | LLM calls | Wall time | Alpha Vantage calls |
|---|---|---|---|
| one run, no mandate, 2 analysts | ~25 | 5–7 min | per vendor config |
| one run, a mandate (adds 2 analysts) | ~30–35 | 7–9 min | ~4 per name for value tools |
| backtest | runs × cells | runs × cells | same, per cell |
| screen, full universe | 0 | minutes | 1 + a few per `--budget` name |
| screen-review | 0 | seconds | 0 |

Measured: a KO `equity_value` run with market + fundamentals took 462 s on
Anthropic (Opus deep, Sonnet quick). A 40-cell backtest is therefore most of an
afternoon; size grids with `--every` and `--analysts` accordingly.

The screener paces Alpha Vantage below the ~257 requests/minute ceiling
measured on this key, and retries a throttled call rather than recording the
name as unavailable.

---

## Caveats and troubleshooting

**Historical screens see companies that later died.** A past `--date` uses the
listings active on that date (back to 2010-01-01); Yahoo has no history for
delisted names, so their prices come from Alpha Vantage; Alpha Vantage drops their
statements, so those come from SEC EDGAR; and grading settles a name that stopped
trading mid-horizon at its last trade. What EDGAR cannot answer -- IFRS filers,
names matching no single filer -- is counted in the **Survivorship** line each
historical screen prints. EDGAR requires `SEC_EDGAR_USER_AGENT` in `.env` (a name
and contact email). Estimate revisions are not filed, so the momentum Growth
analyst's revision signal stays NO DATA for delisted names.

**A value backtest on recent dates looks empty.** It is not broken: cells settle
504 trading days after their date. See the horizon table above, and read the
`REVIEW` blocks in the sweep's log.

**`UNAVAILABLE` in a Quality or Valuation report.** The tool could not load data
— usually a missing `ALPHA_VANTAGE_API_KEY`, a rate limit, or a ticker Alpha
Vantage does not cover (most non-US listings). The analyst is instructed to
report the gap; the rest of the run proceeds.

**A re-run did not appear in the log.** Same ticker, date and mandate is
deliberately not logged twice. Use `--supersede` if the re-run should replace it.

**A backtest sweep "did nothing".** Re-using a `--run-id` skips cells already
done under the same mandate. Use a new run id, or a different mandate.

**Your `.env` changes what tests see.** The package loads `.env` on import; the
test suite now isolates itself from it, but a script that imports
`tradingagents` inherits every `TRADINGAGENTS_*` value in it.

**Which mandates exist?** `equity_value` and `equity_momentum`; an unknown name
fails at startup rather than silently running the wrong style.
