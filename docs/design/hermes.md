# Hermes: an operator agent, and learning graded by outcomes

Status: design, not built. Surveyed 2026-09-25 against NousResearch/hermes-agent
`59004a6` (2026-09-24) and LLMQuant/llmquant-hermes at its default branch.
Companion to `llmquant.md`: the watchers below read the harvest data, and the
lessons below can be served by its knowledge layer.

## The question

Hermes calls itself "the self-improving AI agent": it "creates skills from
experience, improves them during use, nudges itself to persist knowledge,
searches its own past conversations". Two things we could want from that:

1. **An agent that runs this system with us**: tells us whether last night's
   run worked, flags what needs attention, answers questions about decisions,
   from a phone, without a terminal session open.
2. **A system that gets better at investing from its own results**: lessons
   that accumulate, are kept only when they are right, and change how the
   agents decide.

The short answer: Hermes is a good fit for the first, used as a separate
process over MCP. For the second, its learning loop is the wrong signal -- it
learns from its own reading of a conversation, not from whether a call made
money -- but its mechanics are worth copying into a loop we grade with settled
outcomes and random controls.

## What Hermes is (surveyed)

A complete personal-agent runtime: CLI, TUI, desktop app, and a messaging
gateway (Telegram, Discord, Slack and others), with scheduled jobs, MCP servers
as tools, subagents, an approval system and many model providers including
Anthropic (`agent/anthropic_adapter.py`). MIT licence. Very large (about 2,300
Python modules), very active (last commit the day before this survey), and
unversioned (`version = "0.0.0"` in `pyproject.toml`).

### The learning loop, as built

| Claim | Mechanism | Where |
|---|---|---|
| "Nudges itself to persist knowledge" | After a turn, a forked copy of the agent replays the conversation and is asked whether any memory or skill should be saved or updated; it writes straight to the stores | `agent/background_review.py` (`_MEMORY_REVIEW_PROMPT`, `_SKILL_REVIEW_PROMPT`) |
| "Creates skills from experience" | The same review, or `/learn`, writes a `SKILL.md` (agentskills.io format) through the `skill_manage` tool | `agent/background_review.py`, `agent/learn_prompt.py` |
| "Skills self-improve during use" | The review patches existing skills; a **curator** runs when the agent is idle and may pin, archive, consolidate or patch agent-made skills | `agent/curator.py` |
| Memory | `MEMORY.md` (facts) and `USER.md` (the user), edited by the agent | `agent/memory_manager.py` |
| "Searches its own past conversations" | SQLite FTS5 over sessions, with model summaries | `hermes_state_fts.py`, `hermes_state_search.py` |
| User modelling | Honcho, an external service | optional provider |
| Training data | Batch runs over prompt sets and trajectory compression for training tool-calling models | `batch_runner.py`, `trajectory_compressor.py` |

What this settles:

1. **"Self-improving" means prompt files, not weights.** Every mechanism writes
   markdown the next session reads: memory entries and skills. The model does
   not change.
2. **The signal is the model's own judgement.** The review prompt asks the
   model whether anything "stands out" and is "worth saving". Nothing compares
   a lesson with an outcome. That is sound for procedures -- how to rerun a
   failed cell -- and unsound for market judgement, where a confident, plausible
   lesson is exactly the kind that is wrong.
3. **Its curation rules are good and transferable.** The curator only touches
   agent-made skills, never deletes (archives, recoverably), and never
   auto-changes a pinned skill. The skill-writing rules say a lesson is "a
   generalizable rule + one clause of WHY", "not a narrative of what happened
   this session", and "the same lesson learned twice is ONE rule".
4. **It is a runtime, not a library.** Importing it would bring its whole
   surface into this repo. Across a process boundary it costs nothing: Hermes
   takes MCP servers as tools (`mcp_servers` in its config), and this repo can
   be one.
5. **It already has what an operator needs**: a cron scheduler run by the
   gateway daemon (`cron/`), delivery to a phone, approvals before actions
   (`tools/approval*.py`), and per-session tool restrictions (`toolsets.py`).
6. **llmquant-hermes is a set of playbooks, not code**: five scheduled
   "watchers" (morning brief, earnings watch, 13F diff, portfolio pulse,
   Polymarket watch) plus the 18 LLMQuant skills, all on LLMQuant Data, tuned
   for DeepSeek. The watcher shapes are useful; we would point them at our own
   data.

## Shape

```
  phone (Telegram / Slack)
          │  questions, alerts, approvals
          ▼
  ┌────────────────────────────┐   learns: operating procedures,
  │ Hermes (separate process)  │   your preferences (its own memory
  │ gateway · cron · memory    │   and skills; operational stakes)
  └─────────────┬──────────────┘
                │ MCP (stdio)
                ▼
  ┌────────────────────────────┐        ┌───────────────────────────────┐
  │ tradingagents mcp serve    │ reads  │ logs, reports, memory log,    │
  │ read tools + gated actions │───────▶│ screens, data store, harvest  │
  └────────────────────────────┘        └───────────────┬───────────────┘
                                                        │ settled outcomes
                                                        ▼
                                        ┌───────────────────────────────┐
                                        │ outcome-graded learning (ours)│
                                        │ distill → evidence gate →     │
                                        │ test vs controls → promote    │
                                        └───────────────────────────────┘
```

Part 1 (Hermes as operator) and part 2 (outcome-graded learning) are
independent. Part 1 is operations; part 2 is the investing.

## Part 1: Hermes as the operator

### A trade-agents MCP server

`tradingagents mcp serve`, a stdio MCP server in this repo. Hermes, Claude Code
or any MCP client can use it. Its tools wrap what the CLI already does:

| Tool | Returns | Kind |
|---|---|---|
| `nightly_status(date?)` | the night's log summary: start and finish times, screen result, cells run, failed cells with reasons, exit code | read |
| `screen_review(mandate?)` | the picks-vs-controls table and verdict | read |
| `list_decisions(ticker?, mandate?, since?)` | logged decisions with rating, status, outcome | read |
| `get_report(ticker, date)` | a decision's reports (analysts, debate, trader, PM) | read |
| `pending_reviews(within_days)` | decisions whose interim or final review falls due | read |
| `harvest_status()` | snapshot freshness, backfill coverage, request budget used | read |
| `data_store_stats()` | store size, final vs expiring rows, hit rate | read |
| `search_knowledge(query, ticker?)` | knowledge-layer passages (if installed) | read |
| `screen_run(screen_id, max_names)` | adjudicates within a bound | **action, needs approval** |
| `retry_failed(date)` | re-queues the night's failed cells | **action, needs approval** |

No tool places, sizes or simulates an order. The server never returns an API
key; secrets stay in this repo's `.env`.

### Watchers

Hermes cron jobs that call the MCP tools and send a short message:

| Watcher | When | Says |
|---|---|---|
| Nightly health | 08:00 daily | ran or not, how long, cells run and failed, exit code; silent-failure patterns (started 02:00 but worked at 07:00; every cell failed) |
| New decisions | after the nightly run | tickers decided, rating, one-line thesis, link to the report |
| Reviews due | weekly | interim and final reviews coming due in the next 30 days |
| Edge report | weekly | `screen_review` changes: new settled cells, edge by mandate |
| Earnings on names we hold a view on | daily | decided names reporting this week (harvest: `EARNINGS_CALENDAR`) |
| 13F and congress activity | on new data | institutions or members of Congress trading a decided name (harvest: holdings snapshots, `CONGRESS_TRADES` by filed date) |

The first four need only part 1. The last two need the harvest from
`llmquant.md`.

The nightly-health watcher would have answered, unprompted, the question that
found the price-history bug: nine failed cells and an exit code of 0.

### What Hermes learns here, and why that is acceptable

Its own memory and skills fill with operating knowledge: which failures are
routine, how to read the log, what you want to be told and what you do not. The
stakes are operational, every change is visible (`hermes journey`), and the
curator archives rather than deletes. None of it reaches the trading agents.

### Isolation

- A dedicated Hermes profile whose toolset is this MCP server plus messaging.
  No terminal, browser or file tools: news, transcripts and reports reach Hermes
  as tool output, and an agent that reads untrusted text should not also hold a
  shell.
- Both action tools go through Hermes' approval flow and are bounded
  (`max_names`).
- A small model is enough for operations (Haiku 4.5 or Sonnet 5 through the
  Anthropic adapter); Hermes' model bill is separate from the adjudication
  budget.

## Part 2: learning graded by outcomes (ours, on Hermes' pattern)

### What exists

Every decision writes a reflection when it settles and at each interim review.
`get_past_context` (`agents/utils/memory.py:154`) injects up to five same-ticker
and three cross-ticker lessons into the Portfolio Manager's prompt, filtered to
the mandate and to outcomes known by the analysis date. Lessons stay raw text:
nothing merges them, checks them, or retires a wrong one.

### The loop

```
settled outcomes ─▶ distill ─▶ evidence gate ─▶ test as overlay ─▶ promote / archive
(reflections,       rules +     each rule cites   same screens,      new playbook version,
 reviews, alpha)    why         cells for and     --as, vs controls  or the rule is archived
                                against           and vs base        with its record
```

1. **Distill.** Weekly, one model call per mandate reads the settled lessons
   and the current playbook, and proposes changes to a **mandate playbook**: a
   markdown file in skill format, under Hermes' writing rules -- a rule plus one
   clause of why; no incident narration; the same lesson twice is one rule;
   strengthen an existing rule rather than append a copy.
2. **Evidence gate.** Every rule lists the settled cells that support it and
   the ones that contradict it, by id. A rule with fewer supporting cells than
   the bar (a count across distinct tickers and screens, fixed before the loop
   starts) stays a *candidate* and never reaches an agent.
3. **Test.** A candidate playbook becomes an overlay: the mandate with the
   playbook added to its `agent_guidance`, built with `dataclasses.replace` as
   `equity_momentum_leaps` is. It runs against the base mandate on the same
   screens with `screen-run --as`, on screens dated **after** the evidence it
   was distilled from, so the test is out of sample.
4. **Promote or archive.** Promote when the overlay's edge over controls beats
   the base mandate's and keeps doing so across screens; otherwise archive the
   rule with its record. Hermes' curator rules apply: never delete, only
   archive; pinned rules (written by a person) are never changed by the loop;
   a periodic pass consolidates overlapping rules.

### Point-in-time, again

A playbook is knowledge with a date. Each version carries
`available_at` = the latest resolution date among the outcomes it was built
from. A historical run uses the newest version available before its analysis
date. Without that, a backtest of a learned mandate is graded on outcomes it
was taught -- the same look-ahead `get_past_context` already guards against,
one level up.

### Where the rules reach

- The promoted playbook goes into `agent_guidance`, so every role it names sees
  it -- not only the Portfolio Manager.
- Individual lessons stay searchable through `search_knowledge` (`Lesson` cards
  in `llmquant.md`, layer 2), so an analyst can ask "what went wrong last time
  we bought a name like this".

### Cost and pace

Distilling is a handful of model calls a week. Testing spends adjudication
budget, so candidate overlays queue into the nightly `--max-names` like any
other screen. The binding constraint is not cost but evidence: a value call
settles after three years, so the loop leans on interim reviews (126, 252 and
504 days) and on momentum, whose 126-day horizon feeds it fastest.

## Later: decisions as training data

Each cell is a labelled trajectory: the inputs, the agents' reasoning, the
decision, and the realised alpha. Hermes' batch runner and trajectory
compressor define an export format for training tool-calling models. Deferred:

- about 175 settled and pending cells today, far too few;
- a model trained on past outcomes learns the outcomes, and models already
  carry post-2019 market history from pretraining, which also clouds any
  backtest of LLM judgement ("Time Travel is Cheating", arXiv 2505.11065).
  The screener's random controls blunt this for the edge measurement, because
  picks and controls face the same contamination; a fine-tuned model would not
  have that protection.

## Not used, and why

- **Hermes as the trading brain.** The LangGraph pipeline, mandates and
  point-in-time guards are the system; Hermes supervises it.
- **Hermes' own learning loop for investment rules.** Unverified by outcomes;
  part 2 replaces the signal and keeps the mechanics.
- **Honcho.** It models the user; the operator's preferences fit in Hermes'
  built-in `USER.md`.
- **Hermes as a Python dependency.** Integration is over MCP only, which keeps
  the fork additive over upstream and Hermes' churn outside the repo.
- **llmquant-hermes as installed.** Its watchers run on LLMQuant Data; we take
  their shapes and point them at our MCP server.

## Outcomes we are after

| Outcome | Measured by | Today |
|---|---|---|
| Failures found without asking | Failed or anomalous nights reported by the health watcher before anyone looks | Found by asking, a day later (nine failed cells, exit 0) |
| The system reachable from a phone | Questions answered and approvals given through Hermes | Terminal only |
| Lessons that are checked | Rules with cited evidence; candidates vs promoted vs archived | Raw lessons, none checked, none retired |
| Lessons that reach every role | Roles whose guidance carries promoted rules | Portfolio Manager only |
| Learning that helps, shown | Overlay edge over controls minus base-mandate edge, out of sample | Not measured |
| Backtests of learned mandates stay honest | Historical runs use only playbook versions available before their date | No playbook exists |

## Phases

| Phase | Scope | Done when |
|---|---|---|
| O1 | `tradingagents mcp serve` with the read tools; a Hermes profile restricted to it; nightly-health and new-decisions watchers | The morning message reports a failed night correctly; Hermes cannot reach a shell |
| O2 | Action tools behind approval; reviews-due and edge-report watchers; earnings and 13F/congress watchers once the harvest runs | An approved `screen_run` from a phone completes within its bound |
| P1 | Playbook format, weekly distill, evidence gate, candidate/promoted/archived states, version history with `available_at` | First candidate rules with cited evidence; none reach agents ungated |
| P2 | Overlay testing with `--as` on post-evidence screens; promotion rule; curator pass | First playbook version promoted or archived on its record |

O1 needs no LLMQuant work. O2's last two watchers need `llmquant.md` phase H.
P1 and P2 need nothing from Hermes at all: they borrow its rules, not its code.

## Risks and open questions

- **Hermes churn and size.** Unversioned and moving daily. Pin a commit for the
  operator install and keep the contract to MCP, which both sides treat as
  stable.
- **Prompt injection.** Reports and news are untrusted text. The operator
  profile holds no shell and no write tools, and actions need approval, so the
  worst case is a misleading message, not an action.
- **Learning from too little.** Settled cells are few, and a rule fitted to
  them looks convincing. The evidence bar, out-of-sample testing and the
  control group are the defence; the bar is set before the first distill and
  not lowered to get a result.
- **Rules that entrench.** A promoted rule shapes future decisions, which then
  become its evidence. Tests compare against the base mandate on the same
  screens, not against the playbook's own history.
- **Operator cost.** Hermes runs its own model calls (watchers, background
  review). A small model and daily cadence keep it to cents a day; its review
  can be switched off for the operator profile if it adds nothing.
