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

## Design rules

1. **Compute in Python, interpret in the LLM.** Every ratio, return, and
   percentile is a deterministic tool result. Models are not asked to do
   arithmetic they will get subtly wrong.
2. **Additive over upstream.** New files and injected prompt fragments; avoid
   rewriting upstream prompts in place.
3. **Point-in-time discipline.** Upstream already gates memory lessons by
   resolution date (#1251). Longer horizons make look-ahead leakage easier, not
   harder - hold the line.
