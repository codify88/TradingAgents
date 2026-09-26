# The agent lab

Status: built 2026-09-26. Desk → Agents; CLI `tradingagents agents …`.

## Why

The screen lab made the screens testable. The agents were not: their prompts
live in upstream code, and two pilots on ten 2025-09-05 names showed how much
rides on details nobody could see from Desk:

- no-mandate agents were never told their 5-day holding period, and the
  portfolio manager's schema suggested "3-6 months" (fixed, `6467963`);
- the rating scale is written for managing a held position ("Underweight:
  reduce exposure, take partial profits"), so from a flat book the agents
  rated 9 and then 10 of 10 names bearish; across production decisions they
  rate Buy about 1% of the time;
- the agents read prices, fundamentals and news from yfinance, not the
  premium Alpha Vantage key; macro (FRED) is not configured; and the
  harvested congress, 13F, ETF and earnings-call tools are bound to no agent.

## What it does

| Tab | Shows | Does |
|---|---|---|
| Agents | The twelve agents in graph order: model tier, what each reads, its tools (vendor, description, arguments), and the prompt it was last sent; vendors in use; tools no agent calls | Start a variant for an agent |
| Variants | Named, versioned variants | Edit: per-agent prompt edits (append, prepend, replace an exact passage), models, settings, data vendors, extra tools per analyst; a live preview of what an agent would be sent, additions highlighted, a replace that finds nothing flagged |
| Runs | Each run's rating mix, horizon compliance, Buy/Overweight-minus-rest 5-day alpha (with t), direction hit rate, picks vs controls, cost, edits applied/missed | Estimate, confirm, start (a background job) |
| Suites | Fixed historical cases: standard screens' picks and controls on chosen dates | Build one (price requests only) |

## How edits reach the agents

`agentlab/hook.py`, installed only inside a lab run's process, wraps the chat
model's `invoke` for every provider. The calling agent is the LangGraph node
running the call. Appends and prepends go on that agent's system message (or
its first message); a replace rewrites an exact passage and is counted as
missed when the passage is absent. The first prompt each agent receives per
case is captured, and that capture is what the Agents tab and the preview
show. Extra tools are bound to the analyst and executed by its tool node.
Production and nightly runs never import the hook; no upstream agent file
changes.

## Guards

- A run is its own directory and decision memory: a test never teaches the
  production agents.
- The variant is copied into the run, so a later edit cannot change what a
  run is said to have tested; every save is a version.
- Starting a run needs the confirmed estimate and stays under
  `agentlab_max_cost` ($50) unless the operator goes over it explicitly;
  runs are background jobs, refused 01:30-07:30 and while another job runs.
- Suites warn when dates precede the model's training cutoff (Haiku 4.5:
  July 2025; Sonnet 5: January 2026; Opus 5.5 and Fable 5.1: June 2026).
- Small suites check behaviour; the agents-edge t-stat needs dozens of dates.

## Next

1. A capture run (one case, about $0.35 on Haiku) so every agent's prompt is
   visible, then the flat-book / relative-rating variant against baseline on
   the pilot suite, then on a 40-date suite.
2. Adopting a variant: today a variant lives only in the lab. Promoting one
   into production needs the same hook applied to the nightly run, behind an
   explicit adoption like the screen lab's.
3. Prompt caching (0 cache reads today) to cut the input cost of every run.
