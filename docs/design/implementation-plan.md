# Implementation plan: data store, harvest, operator, learning

Status: plan, for review. Written 2026-09-25. Covers `llmquant.md` (data store,
harvest, knowledge layer, strategy intake) and `hermes.md` (operator agent,
outcome-graded learning) as one piece of work, because they share more than
they differ.

## Progress (2026-09-25)

| Wave | Stream | State |
|---|---|---|
| 0 | decisions, usage baseline, designs committed | done |
| 1 | A data store + throttle | built; first real night 2026-09-26 |
| 1 | B MCP server, Hermes operator, 08:00 report | built; report delivered on demand, first scheduled 2026-09-26 08:00 |
| 1 | S platform service (wake daemon, chain runner) | installed; needs two on-time nights with the lid closed |
| 2 | C harvest, graph, point-in-time tools | built; first run 2026-09-26 after the nightly job (25,000 requests) |
| 2 | D evaluation harness + nightly queue (5 names: 2 backlog, 3 trials) | built; no trial registered yet |
| 2 | E exit-rule study | built; first run: holding beat every exit for value (12 calls) and momentum-LEAPS (21) |
| 3-5 | F-K | not started |

## What the two designs share

Built once, used by both:

| Shared piece | Used by | Consequence for the order |
|---|---|---|
| **Evaluation harness**: run a candidate against its base mandate on pre-registered screens, with controls; a trial log; one promotion bar | value-lenses overlay (llmquant L2), learned playbooks (hermes P2), external strategies (llmquant L4) | Built once, before the first candidate. Every candidate counts toward one trial total, so the bar accounts for everything tried, not per project |
| **Nightly budget** (`--max-names`) | the screen backlog, overlay tests, strategy candidates | A queue decides what the nightly slots go to; without one, experiments starve the backlog or the reverse |
| **Point-in-time rule** (`available_at`) | store responses, harvest rows, knowledge cards, playbook versions | One convention module and one test pattern: "a query as of date D returns nothing that became public after D" |
| **Alpha Vantage throttle** | the nightly run, the harvest, agents during a run | Must hold across processes (the harvest and the nightly job can overlap), so it lives in the SQLite store, not in memory |
| **MCP server** | Hermes, Claude Code, any MCP client | The single read interface; each later piece adds its status tool to it |
| **Lessons** | playbooks (hermes P1), `Lesson` cards (llmquant L3) | One reader over `TradingMemoryLog` with the resolution-date rule; both consume it |
| **Harvest data** | agent tools (llmquant), watchers (hermes) | Harvest before the ownership and earnings watchers |

## Waves

```
Wave 0  decisions · baselines · commit designs
          │
Wave 1  ├─ A. data store + throttle + request counters
        ├─ B. MCP server (read) + Hermes operator + health watcher
        └─ S. platform service: wake scheduling, chained runs         (parallel)
          │
Wave 2  ├─ C. harvest + entity graph + point-in-time tools   ← snapshots start
        ├─ D. evaluation harness + nightly queue
        └─ E. exit-rule study                                            (parallel)
          │
Wave 3  ├─ F. value-lenses overlay (first harness trial)
        ├─ G. playbook distill + evidence gate
        └─ H. action tools + remaining watchers
          │
Wave 4  ├─ I. knowledge layer (quant-mind): news, transcripts, reports, lessons
        └─ J. playbook overlays through the harness
          │
Wave 5  K. strategy intake (data-mcp)
```

Each wave ends with the full test suite green, the nightly job run once
against the change, and a commit per work item. Waves 1 and 2 are the ones
with a clock on them: holdings history starts only when the harvest first runs.

## Wave 0: before code

| # | Item | Output |
|---|---|---|
| 0.1 | Your decisions (below) | Answers recorded in this file |
| 0.2 | Baselines, so "cheaper" and "faster" can be shown | Tokens per cell from provider usage on 3 cells; wall time of `screen` and of a 3-name and a 5-name `screen-run` (how late the nightly run finishes, and so when the harvest starts); Alpha Vantage requests per run by endpoint (from the counters in A.3, run once before the store is switched on) |
| 0.3 | Commit `llmquant.md`, `hermes.md` and this plan | One docs commit |
| 0.4 | Branching | One branch per wave off `main` (`feat/datastore`, `feat/harvest`, ...), a PR per wave. The current branch merges first |

## Wave 1

### A. Data store (llmquant L1)

| # | Work | Where | Size |
|---|---|---|---|
| A.1 | `response`, `snapshot`, `node`, `edge`, `throttle` tables; WAL; open/migrate | `tradingagents/datastore/store.py` (new) | M |
| A.2 | Freshness policy per endpoint (final / TTL / snapshot), from the llmquant table | `tradingagents/datastore/policy.py` | S |
| A.3 | Wrap `_make_api_request`: key without `apikey`, lookup, fetch on miss, store raw body; per-endpoint request counters | `dataflows/alpha_vantage_common.py` | M |
| A.4 | Cross-process throttle: a token bucket row in SQLite, 150/min by default (decision 2), from config | `datastore/throttle.py` | S |
| A.5 | Modes `read_write` / `replay` / `off`; config keys; env override | `default_config.py` | S |
| A.6 | EDGAR `_cached_json` onto the store; retire the 3-day `av_daily` cache | `dataflows/sec_edgar.py`, `mandates/tools/financials.py` | M |
| A.7 | Nightly log prints store size and the night's hits, misses and requests by endpoint | `scripts/nightly.sh`, a `tradingagents store-stats` command | S |

Tests: final rows served with no request; expired rows refetch; replay raises
on a miss; `apikey` never stored; corrupt row is a miss; two processes share
the throttle; counters match requests made. Yahoo's CSV cache stays as is
(fixed this week) and moves onto the store later if it earns it.

Done when a second run of the same historical screen makes no requests for
final responses, and a backtest cell passes in replay mode offline.

### B. Operator (hermes O1)

| # | Work | Where | Size |
|---|---|---|---|
| B.1 | `[mcp]` extra (`mcp` package); `tradingagents mcp serve` over stdio | `pyproject.toml`, `tradingagents/mcp_server/` (new) | S |
| B.2 | Read tools: `nightly_status`, `screen_review`, `list_decisions`, `get_report`, `pending_reviews` | `mcp_server/tools.py`, reusing CLI internals | M |
| B.3 | Nightly log parser shared by the tool and the CLI (start, finish, cells, failures, exit code, "started 02:00 but worked at 07:00") | `tradingagents/ops/nightly_log.py` | S |
| B.4 | Hermes install pinned to a commit; operator profile with this MCP server and Telegram only (decision 6), no terminal/browser/file tools; small model | Hermes config (outside the repo), documented in `docs/operator.md` | S |
| B.5 | Watchers: nightly health (08:00), new decisions (after the run) | Hermes cron jobs | S |

Tests: each tool against fixture logs and memory files (last night's real log
is one fixture: nine failed cells, exit 0); the server never returns an
environment variable. Hermes itself is checked by hand once: the profile
cannot run a shell command.

Done when the morning message reports a failed night correctly.

### S. Platform service (decisions 7 and 8)

The whole stack -- nightly run, harvest, Hermes and its watchers -- runs as one
service that wakes the Mac for its own schedule. launchd alone cannot wake a
sleeping Mac, and `pmset repeat` holds a single daily wake, which cannot cover
several jobs.

| # | Work | Where | Size |
|---|---|---|---|
| S.1 | Schedule registry: every timed job in one file (nightly 02:00, harvest chained after it, watchers such as 08:00, weekly jobs), read by everything below | `tradingagents/ops/schedule.py` (new) | S |
| S.2 | Wake daemon: a small root LaunchDaemon that, at load and every 30 minutes, makes sure the next scheduled job has a `pmset schedule wake` a few minutes before it, and cancels wakes it set that are no longer needed. Idempotent, so a missed tick costs nothing. It leaves `pmset repeat` entries alone: the existing daily 01:55 repeat covers the nightly, and `--uninstall` keeps it | `scripts/wake-daemon.sh`, `com.jeremysmith.tradingagents.wake.plist` | M |
| S.3 | Chain runner: nightly then harvest in one launchd job under `caffeinate -i`, so the Mac cannot drop back to sleep between them; each step logs separately and a failed step does not skip the next | `scripts/platform-run.sh` | S |
| S.4 | Hermes gateway as a `KeepAlive` launchd agent: restarts if it dies, resumes after wake | plist, `docs/operator.md` | S |
| S.5 | One installer: `sudo scripts/install-platform.sh` installs the wake daemon (root) and the user agents; `--uninstall` removes all of it and cancels the wakes it set | `scripts/install-platform.sh` | S |
| S.6 | Missed-run detection: the nightly log parser (B.3) flags a job that started late or never ran; the health watcher reports it | `ops/nightly_log.py` | S |

Tests: the schedule computes the right next wake across midnight, weekends and
DST changes; the daemon's wake list is idempotent (running it twice sets one
wake); the chain runner continues past a failed step and returns its failure.
Checked by hand once: with the lid closed on power, the Mac wakes and the
nightly log shows a 02:0x start.

Scheduled wake is reliable on power. On battery with the lid closed macOS may
skip it; the health watcher then reports a late or missing run rather than
failing quietly.

Done when two consecutive nights start within five minutes of schedule with the
lid closed, and the harvest runs straight after the nightly job without a second
wake.

## Wave 2

### C. Harvest (llmquant H)

| # | Work | Where | Size |
|---|---|---|---|
| C.1 | Dataset registry: the six endpoints, key shape, policy, "public when" rule | `tradingagents/harvest/datasets.py` (new) | S |
| C.2 | `tradingagents harvest`: universe (tier-1 survivors, adjudicated names, their ETFs), order (snapshots due → refreshes → transcript backfill newest first), `--max-requests` | `harvest/run.py`, `cli/main.py` | M |
| C.3 | Snapshot dedup: identical payload to the previous snapshot stored as a pointer | `datastore/store.py` | S |
| C.4 | Fiscal-quarter mapping for transcripts (`EARNINGS` `fiscalDateEnding` + `OVERVIEW` `FiscalYearEnd` → quarter label → `reportedDate`) | `harvest/fiscal.py` | M |
| C.5 | Graph fill: nodes and edges from harvested payloads, each edge with `as_of` and `available_at`; name normaliser for insiders and institutions; politicians by bioguide id | `datastore/graph.py` | M |
| C.6 | Point-in-time tools: `get_congress_trades`, `get_institutional_holdings`, `get_etf_exposure`, `get_earnings_call`; `get_insider_transactions` onto the store with the +2-business-day cutoff | `agents/utils/ownership_tools.py` (new), `news_data_tools.py`, `dataflows/interface.py` (new `ownership_data` category) | M |
| C.7 | Schedule: the harvest is the second step of the chain runner (S.3), straight after the nightly run; `harvest_status` added to the MCP server | `scripts/harvest.sh`, `platform-run.sh`, `mcp_server/tools.py` | S |

Tests: each "public when" rule (a congressional trade filed after D is
invisible at D; an insider trade two business days before D is visible, one
day before is not; a holdings snapshot is visible only 45 days after its
quarter end); empty answers recorded and not re-asked; dedup; budget respected.

Tools are registered but not given to any analyst yet: which role gets which
tool is a mandate decision, made in wave 3 or later.

#### Backfill, measured 2026-09-25

A probe of 60 random names from the screener's own universe, through the store:
28 passed the price tier (so ~2,590 names, matching the screen's 2,592);
survivors have 56.9 reported quarters since 2010 on average (10 to 67); about
70% of those quarters have a transcript (22/28 recent, 21/28 five years back,
18/28 twelve years back). Transcript quarters are the company's **fiscal**
quarters (checked on AAPL: its "2025Q1" is the December 2024 quarter). The
listing holds 5,875 ETFs.

| First pass (once) | Requests | Disk |
|---|---|---|
| Transcripts, 2,592 names x 56.9 quarters (empty answers cost a request too) | ~147,500 | ~1.55 GB |
| `EARNINGS` + `OVERVIEW` per name (to date and label each transcript) | ~5,200 | small |
| Insider, congressional and institutional, one per name each | ~7,800 | ~0.3 GB |
| `ETF_PROFILE`, all 5,875 once | ~5,900 | ~40 MB |
| **Total** | **~166,000** | **~1.9 GB** |

Maintenance, about 14,000 requests a week (~2,000 a night, ~14 minutes):
holdings snapshots weekly; the 500 largest ETFs by net assets weekly and the
rest monthly; insider and congressional trades weekly (daily for decided
names); `EARNINGS` weekly, to learn report dates; each new transcript once its
call has happened; `OVERVIEW` quarterly.

The window: the throttle admits 147 a minute, so the screen's ~5,400 price
histories take ~37 minutes and the harvest starts around 03:15; the Mac must be
awake for the 07:55 wake, so a run stops at 07:30 whatever its budget.

| Pace | Harvest ends | Transcripts to 2023 | To 2019 | Complete |
|---|---|---|---|---|
| **25,000/night (decided)** | ~06:10 | ~night 3 | ~night 5 | **~8 nights** |
| 10,000/night | ~04:25 | ~night 7 | ~night 12 | ~21 nights |

Order, at any pace: holdings snapshots first (they cannot be backfilled), then
the `EARNINGS`/`OVERVIEW` that schedule transcripts, insider and congressional
trades, then every quarter for the ~150 names in saved screens and decisions,
then all names newest quarter first.

Done when holdings snapshots run weekly, trades refresh, the transcript
backfill advances within budget, and a historical query returns only rows
public by its date.

### D. Evaluation harness and nightly queue (shared)

| # | Work | Where | Size |
|---|---|---|---|
| D.1 | Trial registry: candidate id, kind (overlay / playbook / strategy), base mandate, pre-registered screen dates, status, created date | `tradingagents/evaluation/trials.py` (new), JSON under `results_dir/trials/` | S |
| D.2 | Comparison: per screen, candidate edge over controls vs base edge over controls, on the same names | `screener/review.py` (extend), `tradingagents evaluate <trial>` | M |
| D.3 | Promotion bar (decision 4, provisional), in one place, applied to every trial and to new hand-written mandates; each verdict records the bar version it was judged under, so a later change never rewrites past promotions; trial count per style shown with every verdict | `evaluation/bar.py` | S |
| D.4 | Nightly queue: `--max-names 5`, 2 backlog and 3 trials (decision 5); unused trial slots fall back to the backlog; `nightly.sh` and the launchd plist move from 3 to 5 names and call the queue instead of `screen-run --all` | `evaluation/queue.py`, `scripts/nightly.sh`, plist | M |

Tests: a candidate screen dated before its evidence is refused; the bar
counts trials across kinds; the queue never exceeds the budget and never
starves either side two nights running.

### E. Exit-rule study (llmquant L2, from LLMQuant's take-profit-lab)

| # | Work | Where | Size |
|---|---|---|---|
| E.1 | For every settled Buy/Overweight: hold to horizon vs trailing stops, tiered exits, volatility-aware exits; the "rollercoaster rate" (reached a large gain, gave most of it back) | `tradingagents/evaluation/exits.py`, `tradingagents exit-study` | M |

No model calls. Prices come through the store. Output is a table by mandate
and rule, with sample sizes, and a plain statement when the sample is too
small to prefer any rule.

## Wave 3

### F. Value-lenses overlay (llmquant L2)

| # | Work | Where | Size |
|---|---|---|---|
| F.1 | `equity_value_lenses`: `equity_value` with investor-lens guidance adapted from LLMQuant skills (Graham, Munger, Marks, Pabrai, Lynch); `NOTICE` entry for the MIT text | `mandates/equity_value_lenses.py`, `NOTICE` | S |
| F.2 | Registered as the first trial; its screens pre-registered; queued | `evaluation/trials` | S |

The first real use of the harness, and the cheapest test of it.

### G. Playbook distill and evidence gate (hermes P1)

| # | Work | Where | Size |
|---|---|---|---|
| G.1 | Lesson reader over `TradingMemoryLog`, applying the resolution-date rule (shared with I.3) | `tradingagents/learning/lessons.py` (new) | S |
| G.2 | Playbook format: skill-style markdown, each rule with why, supporting and contradicting cell ids, state (candidate / promoted / archived / pinned) | `learning/playbook.py` | S |
| G.3 | Weekly distill: one model call per mandate proposes edits under Hermes' writing rules; a deterministic check rejects rules without cited cells | `learning/distill.py`, `tradingagents learn distill` | M |
| G.4 | Evidence gate from your bar; versions with `available_at` = newest resolution date among their evidence | `learning/gate.py` | S |

Nothing reaches an agent in this wave.

### H. Operator actions and watchers (hermes O2)

| # | Work | Where | Size |
|---|---|---|---|
| H.1 | `screen_run(screen_id, max_names)` and `retry_failed(date)`, bounded, behind Hermes' approval | `mcp_server/tools.py` | S |
| H.2 | Watchers: reviews due, weekly edge report, earnings on decided names, 13F and congressional activity on decided names | Hermes cron jobs | S |

## Wave 4

### I. Knowledge layer (llmquant L3)

| # | Work | Where | Size |
|---|---|---|---|
| I.1 | `[knowledge]` extra with quant-mind pinned to a commit; adapter package, the only importer | `pyproject.toml`, `tradingagents/knowledge/` (new) | M |
| I.2 | Ingest: news (final `NEWS_SENTIMENT` rows), transcripts (harvest, as trees), analyst reports (state logs), lessons (G.1) | `knowledge/ingest.py` | M |
| I.3 | `search_knowledge` tool with `available_at_before` fixed to the run date; BM25 only | `agents/utils/knowledge_tools.py` | S |
| I.4 | Historical runs read news from the archive; the backtest summary marks cells whose inputs are fully archived | `dataflows/alpha_vantage_news.py`, `backtest.py` | M |
| I.5 | Measure tokens per cell against the wave-0 baseline | `evaluation/` | S |

### J. Playbook overlays (hermes P2)

| # | Work | Where | Size |
|---|---|---|---|
| J.1 | A candidate playbook becomes an overlay trial on screens dated after its evidence; promotion or archive through the harness; curator pass consolidates rules | `learning/`, `evaluation/trials` | M |
| J.2 | Historical runs load the playbook version available before their date | `mandates/base.py` (guidance injection) | S |

## Wave 5

### K. Strategy intake (llmquant L4)

| # | Work | Where | Size |
|---|---|---|---|
| K.1 | `tradingagents strategy find` over data-mcp paper and wiki search (needs `LLMQUANT_API_KEY`) | `tradingagents/intake/` (new) | M |
| K.2 | Strategy card schema mapped onto `Mandate` fields; one cited extraction call per paper | `intake/card.py` | M |
| K.3 | Feasibility check against the vendors and harvested datasets | `intake/feasibility.py` | S |
| K.4 | Cards stored in the knowledge layer with results; a written-up candidate enters the harness as a trial | `intake/`, `evaluation/trials` | S |

## Decisions I need from you

| # | Decision | My recommendation |
|---|---|---|
| 1 | Does your Alpha Vantage premium agreement allow keeping responses in a permanent local store for personal research? | **Decided 2026-09-25: yes.** `final` rows are kept permanently, with no expiry |
| 2 | Your plan's requests-per-minute limit (for the throttle) | **Decided 2026-09-25: 150/min**, one bucket shared by the nightly run, the harvest and agents mid-run. A 10,000-request harvest takes about 67 minutes |
| 3 | Harvest scope and disk | **Decided 2026-09-25:** tier-1 survivors (~2,600) + adjudicated names; transcripts newest first; no disk ceiling (use what is needed). Pace amended 2026-09-25 after measuring the backfill (below): **25,000 requests a night until the first pass completes (~8 nights), then maintenance, about 2,000 a night**; store size stays in the nightly log (A.7) |
| 4 | Promotion bar for every candidate | **Decided 2026-09-25, provisional:** at least 4 screens with at least 5 settled cells per side each; candidate beats base in at least 3 of 4; +1 screen per 5 trials in the same style. Revisit if nothing clears for months while cells settle (too strict) or promoted candidates stop beating base on later screens (too loose) |
| 5 | Nightly budget split | **Decided 2026-09-25:** `--max-names 5`: 2 backlog, 3 experiments. Backlog pace unchanged; the nightly run gets longer (about 40 minutes of adjudication instead of 25), so the harvest starts later |
| 6 | Messaging channel for Hermes | **Decided and set up 2026-09-25: Telegram.** Private channel; bot is a channel admin that can post (test message delivered); `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are in `.env`, which git ignores. Nothing outstanding for B.4 |
| 7 | Where Hermes runs | **Decided 2026-09-25:** this Mac, and the whole platform runs as one service (stream S) |
| 8 | Scheduled wake so 2am runs at 2am | **Decided 2026-09-25:** a wake daemon re-arms `pmset schedule wake` before every scheduled job (S.2); you run one `sudo scripts/install-platform.sh`, then no manual pmset. Reliable on power. **In place now:** you set `pmset repeat wakepoweron 01:55` daily (confirmed with `pmset -g sched`), so the 02:00 nightly already wakes on time; S.2 treats that repeat as the nightly's wake, adds one-off wakes only for other jobs, and neither changes nor removes it |
| 9 | LLMQuant API key (wave 5 only) | **Requested 2026-09-25, pending.** Nothing before wave 5 waits on it; it goes in `.env` as `LLMQUANT_API_KEY`, never in a commit |

## How we will work

- Each work item is a commit with its tests; each wave a PR you review.
- The suite (1,509 tests) stays green; new modules keep to the fork's rule of
  adding beside upstream, not rewriting it; `quantmind` and `mcp` are optional
  extras, so a plain install is unchanged.
- After waves 1 and 2, one real nightly run checks the change end to end before
  the next wave starts.
- Every wave updates the design docs where the build taught us something.

## Risks to the plan

- **Wave 2 is the widest.** C, D and E are independent and can land in any
  order; C comes first because snapshots cannot be backfilled.
- **Evidence arrives slowly.** Waves 3-5 build machinery whose verdicts depend
  on screens settling. The harness will report "not enough settled cells" for a
  while; that is the correct answer, not a bug.
- **Hermes churn.** Pinned commit, MCP-only contract; if it breaks, the MCP
  server still serves Claude Code and the watchers can move to launchd scripts.
- **quant-mind churn.** Wave 4 is last among the dependencies for this reason;
  if it stalls, the adapter's surface is small enough to reimplement on SQLite
  FTS5.
