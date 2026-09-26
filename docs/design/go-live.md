# Go-live: standard to paper trading first, value and momentum tuned in parallel

Status: plan, for review. Written 2026-09-25. Builds on `llmquant.md`,
`hermes.md` and `implementation-plan.md` (waves 0-3 built).

## The goal

Three strategies, each judged on its own horizon and ladder, taken to live
trading one at a time, fastest feedback first:

| Strategy | Mandate | Horizon | Ladder (market / style / size / skill) | Forward evidence arrives |
|---|---|---|---|---|
| **Standard** | none (upstream agents) | 5 trading days | SPY / SPY / RSP / random controls | **weekly** |
| Momentum | equity_momentum | 126 days | SPY / MTUM / RSP / random controls | months |
| Value | equity_value | 756 days | SPY / IWD / RSP / random controls | years -- goes live on history |

They do not have to beat each other. Each has its own promotion bar and its
own go-live gate. SPY is what we are trying to beat; the other rungs say why we
did or did not.

## What we know (attribution, 2026-09-25, prices only)

- Value, 8 screens: picks +7.3% vs the S&P 500 = style -13.3% + universe -15.0% +
  selection +35.6% (positive on 6/8). Equal-weight lagged by 15.9%, so the
  universe gap is mostly size. The agents' ratings were -21.4% on the 2 rated
  screens -- the layer to watch.
- Momentum, 3 screens: +18.5% = style +8.2% + universe +2.7% + selection +7.6%.
- Standard: no screens yet.

Small samples, heavy tails, before costs. The first job of every phase is to
grow the number of screens each of these rests on.

## Phase 1 so far (screen lab, 2026-09-25)

Every week (standard) or month (momentum) since 2012, from a panel of 8,695
daily histories, delisted names included; tuned before 2020, tested from 2020.

- **Standard**: no ordering clears the bar (32 variants, hurdle t 3.16). The
  best tuned is "the 8 most liquid names" (t 2.99; test +0.45% a week, t 2.3),
  which is mostly the mega-cap run since 2020 rather than a short-term signal.
  Reversal is positive on the test dates (+0.6% a week) but weak in tuning.
  The live screen keeps its default (liquidity) until something clears; for
  standard, the agents are the edge to test, not the ordering.
- **Momentum**: the live ordering works only among the most liquid names.
  Across all ~2,000 survivors, 12-month excess return picked worse than the
  pool in tuning; within the 60 most liquid (as the live screen ranks) it
  picked +4.6% a holding better (t 3.1) and +9.9% on the test dates (t 1.6,
  74 overlapping months). Ranking those 60 by liquidity alone gets most of
  that: the selection beyond size is about +1-2% a holding.
- **Value** (point-in-time statements, 19,680 name-months, median 11 years
  of history): a 3-year hold sampled monthly since 2012 is one or two
  independent bets on each side of the split, so the lab refuses a verdict
  rather than print the t of 13 the overlap produces. On a 1-year read the
  live ordering adds about +2% a year over the quality survivors (t 0.8 in
  both periods): the right sign, not evidence. Value as a style lagged the
  S&P 500 by 16% per 3-year holding over the test dates.
- **Value's quality exclusions drop 73% of the most liquid names**, Apple in
  65% of months and Microsoft in 70%. "Returns on capital deteriorating" trips
  on any downward trend steeper than 0.75pp a year however high the level
  (Apple 44% -> 27% ROIC), and "earnings not backed by cash" trips on
  receivables growth alone with cash conversion at 1.55x. A mandate decision:
  proposed next is to test relative versions of both rules as lab variants.

## Two things to hold onto

1. **Backtests of LLM judgement flatter it.** The models have read about the
   past. Random controls blunt this for the edge (both arms are affected), but
   only forward, out-of-sample trading is clean. Rules (the screener) are much
   less affected, so they carry more weight until forward evidence exists.
2. **Nothing trades without guardrails.** Position and exposure caps, a kill
   switch, and the operator's approval on every order batch, before and after
   paper.

## The standard strategy

### Candidates: a standard screen

The screener needs a mandate today; standard gets its own screen:

- universe: the price tier (price >= $5, dollar volume >= $5M, a year of
  history) -- the same ~2,600 names;
- a short-horizon ordering signal chosen by the screen lab (below) from
  candidates with a published basis: short-term reversal (last week's losers),
  one-month momentum, earnings within the holding window, insider buying in the
  last week (harvest), liquidity only;
- picks and random controls as for every screen, so the ladder applies.

A 5-day horizon means ~700 weekly dates since 2012: the screen lab can test the
standard screen far more thoroughly than the long-horizon ones, in hours.

### Decisions: the plain agents, daily cohorts

Each night, the nightly queue gives standard `K` names from that day's standard
screen (picks and controls). The agents run with no mandate; the portfolio
manager's rating decides entry. Daily cohorts -- one set of names a night, each
held five trading days -- give five times the samples of a weekly rebalance and
keep the book spread across entry days.

### Portfolio: small, equal, capped

| Rule | Default |
|---|---|
| Enter | Buy or Overweight (Overweight at half size) |
| Size | equal weight per cohort; at most 5% of the account per position |
| Book | at most 5 cohorts live (one a day, five days each) |
| Exposure | at most 100% long, no leverage, no shorts in v1 |
| Exit | after five trading days, at the open; no stop in v1 (the horizon is the exit) |
| Kill switch | `tradingagents trade halt` stops all new orders; one Hermes command away |

### Execution: Alpaca paper, approved on Telegram

- **Broker**: Alpaca paper trading (free; the same API as live). Keys in `.env`
  as `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`, paper endpoint only in v1.
- **Order plan**: after the nightly run, the day's entries and exits become an
  order plan (symbol, side, quantity, order type) written to disk.
- **Approval**: the plan goes to Telegram before the open; an action tool
  (`submit_order_plan`, not read-only, so Hermes asks) places it. An unapproved
  plan expires at the open -- nothing is sent by default.
- **Orders**: market-on-open (`opg`) for entries and exits in v1; limit orders
  later.
- **Reconciliation**: each evening, positions and fills vs the plan; any
  mismatch is reported and blocks the next plan until acknowledged.
- **Costs**: commissions are zero on Alpaca; the ladder is also reported after
  an assumed 5 bps a side of slippage, measured against real paper fills.

## Value and momentum in parallel: tuning

- **Screen lab** (no LLM): reruns each screener on many historical dates
  (monthly since 2012 for the long horizons), scores variants on the ladder,
  tunes on early dates and tests on later ones, counts every variant tried, and
  suggests a change the moment one clears the bar on held-out dates. Variants:
  liquidity cut, exclusion thresholds, pick count, ordering signals including
  harvested ones (insider buying, congressional trades, 13F changes, earnings-
  call sentiment). Point-in-time fundamentals are verified first (statements
  only after they were filed).
- **Agent layer on history, cheaply**: on tuned historical screens, compare
  screener-only, screener + agents as a filter, and agents as a veto, with a
  cheap model as a proxy; the expensive model confirms the winner. Priced from
  measured tokens before it runs.
- **Suggestions, on demand and as they happen**: a Telegram message when a
  variant clears the bar; `tradingagents suggest` or "any suggestions?" in
  Hermes at any time. Nothing changes until approved; an approved screener
  change becomes a new, versioned screen config.

## Phases

| # | Phase | Scope | Done when |
|---|---|---|---|
| 1 | Screen lab + standard screen | Historical screen runner, variant registry, walk-forward, point-in-time check, the standard screen with candidate orderings, suggestions | Each strategy's screener has a tuned config chosen on held-out dates; a standard screen runs nightly |
| 2 | Agent layer on history | Cheap-model campaigns per strategy on tuned screens; decide which layers go live | A priced, approved campaign per strategy; a written decision on screener-only vs agents-as-filter vs veto |
| 3 | Portfolio + paper execution | Sizing, caps, order plans, Alpaca paper, Telegram approval, reconciliation, kill switch | A plan approved on Telegram fills in the paper account and reconciles |
| 4 | Paper trading forward | Standard first (weeks), momentum next; value on history plus small size | Criteria you set: tracking vs the ladder, drawdown, costs, behaves as designed |
| 5 | Small real capital | Live endpoint, a capped allocation, approval per batch | Your call |

Standard runs phases 1 -> 3 first so it can paper trade within about two
weeks; value and momentum go through phases 1 and 2 in parallel.

## Decisions needed

| # | Decision | Recommendation |
|---|---|---|
| 1 | An Alpaca paper account and its keys (in `.env`) | Create one at alpaca.markets; paper only |
| 2 | Standard's nightly names `K` (each an LLM run) | 2 a night to start, priced from tonight's measured tokens |
| 3 | Approval | Every order plan approved on Telegram, in paper as in live, until you relax it |
| 4 | Position rules | The defaults above |
| 5 | Phase-2 campaign budget per strategy | Quoted from measured cost before running; cheap model first |
