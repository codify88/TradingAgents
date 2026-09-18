# Investment Mandates

## Why

Upstream TradingAgents is built for a **short-horizon directional trade**. Its
learning loop grades every decision on 5-day alpha vs SPY
(`TradingAgentsGraph._fetch_returns`, `holding_days=5`), its Trader emits a
Buy/Hold/Sell transaction, and its indicator menu (RSI / MACD / Bollinger) is
swing-trade shaped. Run it against a Buffett-style thesis and the memory log
will teach it, correctly and uselessly, that the thesis "failed" because the
stock was flat for a week.

A **Mandate** is a first-class description of *what kind of investor is asking*.
It carries the evaluation horizon, the benchmark, the framing each agent argues
within, the rating semantics, and the hard screens. It is threaded through the
graph exactly the way `asset_type` already is (state -> propagation -> run
signature -> analyst selection), so it composes with the existing crypto mode
rather than competing with it.

## The seam

`asset_type` answers *what* is being analysed. `mandate` answers *on whose
behalf, over what horizon, judged how*. They are orthogonal:

| | `asset_type` | `mandate` |
|---|---|---|
| Values | stock, crypto, (later: option, bond, fx) | equity_value, equity_momentum, ... |
| Selects | which data tools exist | how they are read and graded |

## Mandate fields

| Field | Purpose |
|---|---|
| `horizon_days` | Primary grading horizon for the reflection loop |
| `review_horizons_days` | Interim checkpoints so a 12-month call still produces signal at 1/3/6mo |
| `benchmark` | Overrides `benchmark_map`; a momentum sleeve is judged vs a different index than a value sleeve |
| `thesis_frame` | What the bull/bear debate is actually about |
| `analyst_guidance` | Per-analyst prompt fragment injected alongside `instrument_context` |
| `rating_guidance` | How conviction maps onto the existing 5-tier rating at this horizon |
| `disqualifiers` | Hard screens that force Sell/Hold regardless of the debate |
| `indicator_shortlist` | Which technical indicators are even relevant |
| `risk_frame` | What "risk" means to the trader, risk debate and PM (e.g. permanent capital loss, not volatility) |
| `analysts` | Extra personas the mandate adds to the analyst team, each with its own tools |

Prompts stay upstream's. The mandate is **injected**, never forked, so
`git merge upstream/main` keeps working.

## Phases

- **P1 - scaffolding.** `mandates/` package, state plumbing, mandate-driven
  horizon + benchmark in the reflection loop, CLI selection. *No new analysis
  yet - this is the part everything else stands on.*
- **P2 - `equity_value`.** Alpha Vantage 20yr financials; deterministic
  quality + valuation tools (ROIC/ROE trend, margin stability, capital
  allocation, FCF yield, EV/EBIT vs own history, reverse-DCF); Quality/Moat
  and Valuation analysts; risk debate reframed around permanent capital loss.
- **P3 - `equity_momentum`.** Deterministic relative-strength tool (1/3/6/12mo
  vs benchmark and sector, percentile rank, 52wk-high proximity, volume
  confirmation) and earnings-revision breadth from `EARNINGS_ESTIMATES`;
  Momentum and Growth analysts; explicit kill-criteria in the PM decision.
- **P4 - screener.** Deterministic universe rank from `LISTING_STATUS` so the
  expensive agent graph only runs on the top N names.
- **Later.** LEAPS (`HISTORICAL_OPTIONS`), bonds (`TREASURY_YIELD`), FX
  (`FX_DAILY`) - all covered by the same Alpha Vantage key.

## Mandate analysts and tools

A mandate can add analysts of its own. `equity_value` adds two: a **Quality
Analyst** (is this a durable business?) and a **Valuation Analyst** (does this
price leave a margin of safety?). Each gets only the tools for its half of the
question, so neither drifts into the other's job.

```
tradingagents/mandates/
  tools/        financials.py   point-in-time Alpha Vantage statements, prices
                quality.py      ROIC, ROTC, margins, cash conversion, screens
                valuation.py    multiples vs own history, reverse DCF, screen
                value_tools.py  the four LLM tools, rendered as cited markdown
  analysts/     base.py         MandateAnalyst + the node that runs one
                value.py        Quality and Valuation analysts
  graph.py      splices analysts into the graph; reads their reports back
```

**Why tools live here, not in `dataflows/`.** Upstream's tools are thin shims
over a vendor-pluggable data layer. These are *computations* over vendor data,
not a new vendor; registering them with upstream's router would put every
mandate tool on a file upstream edits often. They reuse upstream's Alpha
Vantage request helper unmodified.

**The extension point.** Adding an analyst node upstream touches six
registries. P2 paid that once, generically, so P3's analysts are purely
additive:

| Upstream file | Hook |
|---|---|
| `agent_states.py` | one `mandate_reports` dict channel with a merge reducer, for every mandate analyst |
| `propagation.py` | `mandate_reports` starts empty |
| `graph/setup.py` | `setup_graph(..., mandate_analysts=())` splices them in after the selected analysts |
| `trading_graph.py` | passes `mandate.analysts`; keys them into the checkpoint signature; logs their reports |
| `agent_utils.mandate_section` | appends their reports, so every downstream agent -- which already reads this section -- sees them with no prompt edit |
| `reporting.py`, `cli/main.py` | one file and one status row per persona |

With no mandate, or a mandate without analysts, every one of these is a no-op.

**Point-in-time statements.** Upstream admits a statement once its fiscal
period has *ended*. That leaks: FY2025 ends 31 Dec but is not public until the
February earnings release. The loader admits a period only once it was
*reported*, using the `reportedDate` Alpha Vantage's EARNINGS endpoint carries,
and falls back to the SEC filing deadline (90 days for a 10-K, 45 for a 10-Q).

**Screens.** The disqualifiers the statements can settle are computed, with
the threshold printed next to the measured value:

| Status | Meaning |
|---|---|
| `TRIPPED` | The numbers meet the disqualifier |
| `WATCH` | They meet it on one reading but not another; the analyst must say which reading holds and why |
| `CLEAR` | They do not |
| `NO DATA` | The statements cannot settle it; the analyst must say so, not guess |

`WATCH` exists because the honest answer is sometimes "it depends on which
year you believe." A screen that trips on annual cash conversion while the
trailing twelve months have recovered is reporting a one-off, not a trend.
Where a verdict depends on an assumption, the screen uses the assumption most
generous to the stock (a 7% discount rate; the best of revenue, operating
income and FCF growth as the record), so a `TRIPPED` never rests on a
contestable input.

**Requires `ALPHA_VANTAGE_API_KEY`.** yfinance carries about four years of
statements, too few to judge durability. Without the key the tools return an
explicit `UNAVAILABLE` notice and the analysts are told to report the gap
rather than fill it from memory.

## Design rules

1. **Compute in Python, interpret in the LLM.** Every ratio, return, and
   percentile is a deterministic tool result. Models are not asked to do
   arithmetic they will get subtly wrong.
2. **Additive over upstream.** New files and injected prompt fragments; avoid
   rewriting upstream prompts in place.
3. **Point-in-time discipline.** Upstream already gates memory lessons by
   resolution date (#1251). Longer horizons make look-ahead leakage easier, not
   harder - hold the line.

## Status

**P1 is landed.** `mandates/` ships `equity_value` and `equity_momentum`; the
mandate reaches every analyst, both researchers, the research manager, the
trader, all three risk debators, and the portfolio manager; the grading horizon
and benchmark follow the mandate; the memory log records which mandate produced
each decision; the CLI asks for one as step 2 and `TRADINGAGENTS_MANDATE` sets
it for unattended runs. The indicator shortlist narrows the market analyst's
twelve-indicator menu to what the horizon can use (three for `equity_value`,
eight for `equity_momentum`), and a shortlist naming an indicator the vendors
don't implement fails at import. With no mandate selected every rendered prompt
is byte-identical to upstream. **P1 is complete**; every `Mandate` field is now
consumed.

**Interim grading is landed.** A pending entry is now checkpointed at every
`review_horizons_days` milestone that has come due, and settled once at the
primary horizon. One log entry therefore holds several outcomes:

```
[2026-09-17 | KO | Hold | pending | mandate:equity_value]

DECISION:
...

REVIEW 63d @ 2026-12-16: raw +3.2% | alpha -1.1%
Tracking but lagging SPY by 1.1pp; at 13% of the horizon that is noise.

REVIEW 126d @ 2027-03-17: raw +8.1% | alpha +2.4%
FCF coverage normalised in Q4; the flag that blocked adding has cleared.
```

Three properties matter:

1. **A checkpoint is not a verdict.** The interim reflection runs off its own
   prompt, which states how much of the horizon has elapsed and forbids calling
   the decision right or wrong. Injected context labels the entry `in progress`.
   Reusing the final-reflection prompt would have manufactured a verdict at 13%
   of the horizon — the same short-termism the mandate exists to remove, just
   relocated.
2. **Point-in-time discipline extends to checkpoints.** A review is visible to a
   later run only once its own resolution date has passed, so a backtest cannot
   read a checkpoint that had not happened yet (#1251).
3. **The unmandated path is untouched.** No mandate means no review horizons,
   so an upstream-shaped run makes exactly the price requests it made before.

Two bugs surfaced on the way and are fixed here: the outcome window asked for
`holding_days + 7` *calendar* days, so a 504-trading-day horizon could never
settle (it requested 511 days for a window that spans ~730); and log rotation
identified pending entries by the tag suffix `| pending]`, which the `mandate:`
marker broke, making unresolved long-horizon work prunable.

**P2 is landed.** `equity_value` runs a Quality Analyst and a Valuation
Analyst on computed, point-in-time evidence (see *Mandate analysts and tools*),
its risk debate is framed around permanent capital loss rather than
volatility, and the upstream fundamentals and market analysts are narrowed so
they no longer re-derive ratios or quote historical ranges from memory -- the
P1 KO run asserted a "normal 20-22x" P/E band for KO; the computed ten-year
range is 23x to 32x.

**Not yet started.** P3 (momentum), P4 (screener).
