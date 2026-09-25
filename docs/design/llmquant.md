# LLMQuant: a point-in-time data store, a knowledge layer, and a strategy intake

Status: design, not built. Surveyed 2026-09-25 against quant-mind `10e9dbd`
(v0.2.0, 2026-08-15), skills, data-mcp, Magents, llmquant-book and
awesome-trading-agents at their default branches that day.

## The question

LLMQuant (github.com/LLMQuant) publishes quant-finance tooling for LLM agents.
Three things we want from it, and the question for each is whether LLMQuant is
the right way to get it:

1. **Cheaper, repeatable runs.** A full sweep of the saved screens is 1,336
   minutes and about 600k model tokens per name. Most Alpha Vantage responses
   are re-fetched every run, and every backtest prints "text feeds are not
   archived, so these figures are indicative rather than repeatable".
2. **Retrieval over what we already know** -- news, filings, our own analyst
   reports and settled lessons -- instead of pasting whole documents into
   prompts, and without leaking the future into a historical run.
3. **A supply of strategies we did not write ourselves**, taken from research
   and tested in this framework against random controls before any of them is
   trusted.

The short answer: LLMQuant supplies the text-retrieval layer (quant-mind), a
research search API (data-mcp) and prompt content (skills). It does **not**
supply the data store: quant-mind's graph is unbuilt, and numbers do not belong
in a text retriever. We build that part ourselves.

## What LLMQuant is (surveyed)

| Repo | What it actually is | Licence | Maturity | Verdict |
|---|---|---|---|---|
| quant-mind | Python library: typed, cited, dated "knowledge cards" in SQLite, BM25 and optional embedding search, LLM flows that turn papers and news into cards | MIT | Real code, ~23k lines, tests + CI, pre-1.0 | **Use** for text, behind an adapter |
| data-mcp | MCP client for the hosted LLMQuant Data API: papers, quant wiki, SEC sections, 13F, ETF holdings, macro, news, prices | MIT | Real code, tested | **Use** for research search only |
| skills | 18 skill packs of markdown workflows; every `scripts/` folder is empty | MIT | Prompts only | **Borrow** specific workflows as prompt content |
| Magents | Multi-agent backtester whose agents come from virattt/ai-hedge-fund; fills at the bar price, costs a flat 10 bps | MIT | Thin | Skip; one idea taken (per-strategy risk budget) |
| llmquant-book | Chinese-language textbook + two teaching notebooks | CC BY-NC 4.0 | Educational | Skip (non-commercial licence) |
| awesome-trading-agents | Curated list | CC0 | List | Reference only (portfolio/execution links, see the end) |

What this settles:

1. **quant-mind's graph does not exist.** `knowledge/_graph.py` is a placeholder
   whose `__init_subclass__` raises `NotImplementedError` ("design-intent
   placeholder; subclassing is blocked until the shape is finalised"). A graph
   of our data has to be ours.
2. **Its point-in-time model is the real asset.** Every card carries a mandatory
   `as_of` and an optional `available_at` (when the source became observable),
   and search filters on both: `SemanticQuery.as_of_before` /
   `available_at_before` (`library/_types.py:42-43`). That is exactly the guard
   a backtest needs, already built.
3. **Its `Factor` and `Thesis` cards are stubs** (one or two fields; "full
   payload lands with factor_flow"). A strategy card has to be ours too.
4. **It is built for text.** The mature flows are papers (arXiv/PDF/DOI) and
   news (RSS, PR Newswire). Nothing in it stores or queries a price series or an
   option chain, and it should not.
5. **data-mcp costs credits beyond a free beta allowance**
   (`LLMQUANT_API_KEY`; "the credit model is in beta -- amounts may change").
   The research tools are cheap: `paper_search` and `wiki_search` 1 credit,
   `paper_read` and `wiki_read` 0. Its market data duplicates our Alpha
   Vantage premium key, which also serves institutional holdings
   (`INSTITUTIONAL_HOLDINGS`) and ETF holdings (`ETF_PROFILE`) -- so data-mcp is
   needed for research search and nothing else.
6. **The skills assume LLMQuant Data and list data it does not have yet**
   ("portfolio risk model output", "exit-rule backtest data"). They are useful
   as structure and guardrails, not as runnable workflows.

## Shape: three layers

```
                ┌────────────────────────────────────────────────────┐
  agents ──────▶│ tools: get_stock_data, options, fundamentals, ...  │
                │        search_knowledge(query, ticker)   [new]     │
                └───────────────┬───────────────────────┬────────────┘
                                │                       │
          numbers               ▼                       ▼            text
   ┌──────────────────────────────────┐   ┌──────────────────────────────────┐
   │ 1. point-in-time data store      │   │ 2. knowledge layer (quant-mind)  │
   │    SQLite, ours, stdlib only     │   │    news, reports, lessons,       │
   │    vendor responses + entities   │   │    filing sections as cards      │
   └───────────────┬──────────────────┘   └───────────────┬──────────────────┘
                   │ misses only                          │ ingest
                   ▼                                      ▼
     Alpha Vantage · EDGAR · Yahoo            AV NEWS_SENTIMENT · state logs ·
                                              TradingMemoryLog · EDGAR
   ┌──────────────────────────────────────────────────────────────────────────┐
   │ 3. strategy intake: data-mcp paper/wiki search → strategy card →         │
   │    human review → mandate or ordering signal → screens vs controls       │
   └──────────────────────────────────────────────────────────────────────────┘
```

Layers 1 and 2 are independent; either is useful alone. Layer 3 uses layer 2 to
store what it finds, and the existing screener to judge it. A **harvest** job
fills layers 1 and 2 ahead of need -- transcripts, insider and congressional
trades, institutional and ETF holdings -- so future ideas find the history
already on disk.

## Layer 1: a point-in-time data store (ours)

### Why ours, and why not RAG

Prices, chains and statements are queried by key -- symbol, endpoint, date --
and the answer must be exact. Retrieval by similarity is the wrong access path
for them, and quant-mind has no table or graph store to put them in. SQLite is
in the standard library, so this layer adds no dependency.

### What it replaces

- Every Alpha Vantage call goes through one function,
  `_make_api_request` (`dataflows/alpha_vantage_common.py:67`). Its callers
  cover 14 endpoints: `TIME_SERIES_DAILY_ADJUSTED`, `HISTORICAL_OPTIONS`,
  `OVERVIEW`, `INCOME_STATEMENT`, `BALANCE_SHEET`, `CASH_FLOW`, `EARNINGS`,
  `EARNINGS_ESTIMATES`, `EARNINGS_CALENDAR`, `DIVIDENDS`, `TREASURY_YIELD`,
  `LISTING_STATUS`, `NEWS_SENTIMENT`, `INSIDER_TRANSACTIONS`.
- Today most of them are cached only for the life of the process
  (`functools.lru_cache` in `mandates/tools/options.py` and `financials.py`).
  The only disk caches are the screener's `av_daily` (kept 3 days,
  `financials.py:353`), EDGAR JSON, and the Yahoo OHLCV CSVs. A historical
  option chain for 2019 is re-downloaded by every process that asks for it,
  though it can never change.

### Schema

One SQLite file, `data_cache_dir/store.sqlite`:

```sql
CREATE TABLE response (
    vendor      TEXT NOT NULL,     -- alpha_vantage | sec_edgar | yahoo
    endpoint    TEXT NOT NULL,     -- HISTORICAL_OPTIONS, ...
    params_key  TEXT NOT NULL,     -- canonical JSON of params, apikey removed
    symbol      TEXT,              -- denormalised for lookups and pruning
    fetched_at  TEXT NOT NULL,     -- UTC
    final       INTEGER NOT NULL,  -- 1 = can never change (see policy)
    payload     BLOB NOT NULL,     -- gzip of the raw body, exactly as served
    PRIMARY KEY (vendor, endpoint, params_key)
);

-- snapshot endpoints (no history on the vendor side): append, never replace
CREATE TABLE snapshot (
    vendor, endpoint, params_key, symbol,   -- as above
    fetched_on  TEXT NOT NULL,     -- local date; one row per endpoint+params per day
    payload     BLOB NOT NULL,
    PRIMARY KEY (vendor, endpoint, params_key, fetched_on)
);

-- entities: the "graph", as two relational tables
CREATE TABLE node (id TEXT PRIMARY KEY, kind TEXT, attrs TEXT);
CREATE TABLE edge (src TEXT, dst TEXT, kind TEXT,
                   as_of TEXT,         -- when the fact was true
                   available_at TEXT,  -- when it became public
                   source TEXT,        -- endpoint + params_key it came from
                   attrs TEXT,
                   PRIMARY KEY (src, dst, kind, as_of));
```

Raw bodies are stored, not parsed frames, so a parser fix never needs a
re-fetch and the store never encodes a bug.

### Freshness policy (per endpoint)

The rule that saves the calls: **a response about a closed past period is
final.** Everything else gets a TTL.

| Endpoint | Final when | Otherwise |
|---|---|---|
| `HISTORICAL_OPTIONS` (date given) | the date is before today | TTL 1 day |
| `LISTING_STATUS` (date given) | the date is before today | TTL 1 day |
| `NEWS_SENTIMENT` | `time_to` is more than 7 days ago (late-indexed articles settle) | TTL 1 hour |
| `TIME_SERIES_DAILY_ADJUSTED` | never: it grows daily and future splits re-adjust the past | TTL 1 day |
| Statements, `OVERVIEW`, `EARNINGS*` | never: restatements and new quarters | TTL 1 day |
| `TREASURY_YIELD`, `DIVIDENDS`, `EARNINGS_CALENDAR` | never | TTL 1 day |
| `EARNINGS_CALL_TRANSCRIPT` (quarter given) | the quarter's call has happened (its `EARNINGS` report date is past) | TTL 1 day |
| `INSIDER_TRANSACTIONS`, `CONGRESS_TRADES` | never: full history in one response, growing | TTL 1 day |
| `INSTITUTIONAL_HOLDINGS`, `ETF_PROFILE` | never, and the vendor keeps no history | **snapshot**: appended to `snapshot`, one per week |
| `POLITICIAN_METADATA` | never | TTL 7 days |

Point-in-time correctness does not come from the store: statements carry their
own period and filing dates, and the tools already cut at the analysis date.
The store only remembers what the vendor said and when we asked.

### Modes

- `data_store: "read_write"` (default): hit returns the stored body; miss or
  expired fetches, stores, returns.
- `data_store: "replay"`: never fetch; a miss raises. A backtest cell run in
  replay mode is provably not using anything new, which is the numeric half of
  repeatability.
- `data_store: "off"`: today's behaviour, for debugging.

The `lru_cache` layers stay in front as the in-process cache; the `av_daily`
3-day cache is retired into the store.

### The entity graph, and what it is for

Every edge carries `as_of` (when it was true) and `available_at` (when it became
public), so a historical query walks only the graph as it was known then.

| Node | From |
|---|---|
| ticker, sector, industry | `OVERVIEW`, `LISTING_STATUS` |
| insider (person) | `INSIDER_TRANSACTIONS` (`executive`, `executive_title`) |
| politician | `POLITICIAN_METADATA` (bioguide id, party, chamber, state, aliases) |
| institution | `INSTITUTIONAL_HOLDINGS` (`holder_name`) |
| ETF | `ETF_PROFILE` |

| Edge | Meaning | `available_at` |
|---|---|---|
| ticker `in_sector` sector | classification | fetch date (not point-in-time; see risks) |
| insider `officer_of` ticker | title held | first transaction's public date |
| insider `traded` ticker | one Form 4 row: shares, price, A/D | transaction date + 2 business days |
| politician `traded` ticker | one disclosure: amount range, type, owner | `filed_date` |
| institution `holds` ticker | shares, change, as of a quarter end | quarter end + 45 days (13F deadline) |
| ETF `holds` ticker | weight | snapshot date |

Its first consumers:

- the knowledge layer, to resolve "news mentions ticker" and to widen a search
  to a name's sector peers;
- agent tools: "who has been buying", "which funds added", "which ETFs overlap";
- a future portfolio-construction layer (sector caps, ETF overlap), which is
  its own design.

It stays two tables until a query needs more. No graph database; quant-mind's
`GraphKnowledge`, if it ships, can be fed from these tables.

### Tests

The store is keyed and deterministic, so it tests without a network: final
responses are served without a request; expired ones refetch; replay raises on
a miss; `apikey` never reaches `params_key`; a corrupt row is a miss, not a
crash; concurrent writers (two screen processes) do not corrupt the file (WAL
mode).

## Harvest: pulling ahead of need

The store fills lazily when agents ask. Some data cannot wait for that:
institutional and ETF holdings have **no history on the vendor side**, so every
week not snapshotted is a week no future backtest can ever see. And a new idea
should not start by waiting weeks for a backfill. A separate job pulls more than
today's agents use.

### Datasets (probed 2026-09-25 with our key)

| Endpoint | Key | History | Public when | Size (gz) |
|---|---|---|---|---|
| `EARNINGS_CALL_TRANSCRIPT` | symbol × fiscal quarter | 2010Q1 on; turn by turn (speaker, title, text, sentiment) | the call date, taken from `EARNINGS` `reportedDate`; the response carries no date | ~15 KB per call |
| `INSIDER_TRANSACTIONS` | symbol | all of it in one response (IBM: 4,413 rows, to 2012) | transaction date + 2 business days: no filing date is reported (as `y_finance.py:467` already warns) | ~40 KB per symbol |
| `CONGRESS_TRADES` | symbol, or politician | all of it (AAPL: 650 trades) | `filed_date` -- weeks after `transaction_date` (e.g. traded 08-20, filed 09-17) | ~17 KB per symbol |
| `INSTITUTIONAL_HOLDINGS` | symbol | **latest only**, one row per holder with `last_reported` | quarter end + 45 days | ~80 KB per snapshot |
| `ETF_PROFILE` | ETF | **latest only**: holdings with weights, sectors (SPY: 496 holdings) | snapshot date | ~7 KB per snapshot |
| `POLITICIAN_METADATA` | -- | 1,146 members with aliases and terms | -- | ~80 KB |

What this settles:

1. **Transcripts need the earnings calendar to be point-in-time.** The fiscal
   quarter maps to `EARNINGS` `fiscalDateEnding`, and its `reportedDate` is when
   the call was public. Fiscal quarters are the company's own (Apple's Q3 ends in
   June), so the mapping uses `OVERVIEW` `FiscalYearEnd`.
2. **Congressional trades must be dated by filing.** Dating them by trade would
   hand a backtest a disclosure weeks before anyone could read it.
3. **Holdings snapshots start today or never.** Their history begins with the
   first harvest. EDGAR 13F filings (free; our EDGAR client exists) are the way
   to backfill institutional history later, if an idea needs it.
4. **Politician names resolve.** `POLITICIAN_METADATA` carries aliases and
   bioguide ids, so a congressional trade attaches to one node. Insider and
   institution names are free text and need a normaliser (see risks).

### The job

`tradingagents harvest`, scheduled next to the nightly run but separate from
it: it makes no model calls, so it never competes for the adjudication budget.

- **Universe**: every name that passed tier 1 of any screen (about 2,600
  today), every name ever adjudicated, and the ETFs in `ETF_PROFILE` coverage
  that hold them.
- **Order each run**: snapshots due first (holdings are the only thing that is
  lost by waiting), then incremental refreshes (insider and congressional
  trades, TTL 1 day), then the transcript backfill, newest quarters first,
  within a per-run request budget.
- **Budget**: `HARVEST_MAX_REQUESTS` per run, kept under the plan's per-minute
  limit by a client-side throttle in `_make_api_request` (there is none today;
  a rate-limit notice raises `AlphaVantageRateLimitError`).

Size of the backfill, for the ~2,600-name universe:

| Dataset | Requests | Disk (gz) |
|---|---|---|
| Transcripts, 2010Q1-2026Q2 (at most 66 quarters a name; fewer for younger listings) | up to ~170,000 once, then ~2,600 a quarter | up to ~2.6 GB |
| Insider + congressional trades | ~5,200 a day at TTL 1 day; weekly is enough outside screen names | ~150 MB |
| Institutional holdings snapshots | ~2,600 a week | ~200 MB a week, before dedup |
| ETF profiles | a few hundred a week | small |

Institutional holdings only change when a 13F is filed, so a snapshot identical
to the last one for that symbol is stored as a pointer to it. That keeps the
weekly figure far below 200 MB outside filing season. Transcripts are the only
large backfill, and a per-run budget spreads them over weeks.

### How agents reach it

Point-in-time tools over the store, each cutting at the run's analysis date by
the "public when" rule above:

- `get_insider_transactions` (exists; moves onto the store and gains the
  +2-business-day cutoff);
- `get_congress_trades(ticker)`: trades filed on or before the date, amount
  ranges as reported;
- `get_institutional_holdings(ticker)`: the latest snapshot whose holdings were
  public by the date, with changes; says so when no snapshot is old enough;
- `get_etf_exposure(ticker)`: ETFs holding the name, from the nearest snapshot;
- transcripts through `search_knowledge` (layer 2), plus
  `get_earnings_call(ticker, quarter)` for a whole call.

Which roles get which tool is a mandate decision (`agent_guidance` and the
analyst's tool list), made when an idea needs it. Harvesting does not wait for
that.

## Layer 2: the knowledge layer (quant-mind, optional)

### Packaging

quant-mind is v0.2.0, installed from source, with heavy dependencies
(`openai-agents`, `llama-index-core`, `litellm`, `pymupdf`). It goes behind an
optional extra, `pip install -e .[knowledge]`, pinned to a commit, and behind
one adapter package, `tradingagents/knowledge/`. Nothing else imports
`quantmind`. Without the extra, the new tool reports "knowledge layer not
installed" and every run behaves as it does today. This keeps the fork additive
over upstream.

### What goes in

Ingest is deterministic: no model calls are needed to file what we already
have.

| Source | Card | `as_of` | `available_at` |
|---|---|---|---|
| Alpha Vantage `NEWS_SENTIMENT` articles | quant-mind `News` (headline, timestamp, entities, sentiment; body kept as the source text) | published time | published time |
| `EARNINGS_CALL_TRANSCRIPT` calls | quant-mind `TreeKnowledge`: call -> prepared remarks / Q&A -> turns (speaker, title, sentiment) | fiscal quarter end | call date (`EARNINGS` `reportedDate`) |
| Our analyst reports, from `full_states_log_<date>.json` per cell | `AnalystReport` (ours, a `FlattenKnowledge` subclass): ticker, mandate, role, text | analysis date | analysis date |
| Settled lessons, from `TradingMemoryLog` reflections | `Lesson` (ours): ticker, mandate, rating, outcome, text | analysis date | **resolution date** |
| 10-K / 10-Q sections from EDGAR (later) | quant-mind `TreeKnowledge` | period end | filing date |

The `Lesson` row is the one to get right. Lessons already reach the Portfolio
Manager only once their outcome is known: `get_past_context`
(`agents/utils/memory.py:154-176`) keeps an entry only if its `resolved` date
is on or before the analysis date. The card must carry that as
`available_at`, and every search in a historical run filters
`available_at_before = analysis_date`.

### How agents use it

One tool, `search_knowledge(query, ticker=None, kinds=None, k=8)`, wrapping
`LocalKnowledgeLibrary.search(SemanticQuery(...))` with `available_at_before`
fixed to the run's analysis date. The agent cannot widen that cutoff. BM25 first
(no embedding model, no cost); embeddings only if BM25 retrieval measurably
misses. quant-mind's library is async, so the tool runs it in the graph's event
loop or via `asyncio.run`.

What changes for the agents:

- **Analysts** read the top passages for their question instead of up to 20
  full articles (`news_article_limit`), and can find an earlier report on the
  same name.
- **Lessons reach more roles.** Today only the Portfolio Manager sees past
  context. Analysts and the risk debators can search lessons for the ticker,
  its sector peers and the mandate.

### Repeatable backtests

In a historical run the news tools read from the archive (layer 1's final
`NEWS_SENTIMENT` responses, filed as cards) rather than asking live. Two runs of
the same cell then see the same text. Together with replay mode for numbers,
the caveat on every backtest -- "text feeds are not archived" -- can be
dropped for cells whose inputs are fully archived, and the summary says which
cells those are.

### What we do not use from quant-mind (yet)

Its LLM extraction flows for news (we have structured news already), the
agentic retriever (our agents are the agents), `GraphKnowledge`, `Factor`,
`Thesis`. Revisit `Factor` and `Thesis` when `factor_flow` ships.

## Layer 3: a strategy intake

### The pipeline

```
find ──▶ extract ──▶ feasibility ──▶ human review ──▶ evaluate ──▶ promote / archive
data-mcp   strategy    can our data    a mandate or      historical     edge persists vs
paper/wiki card (LLM,  compute every   ordering signal   screens with   controls, or the
search     cited)      input as of     is written by     random         card is filed
                       the date?       hand, as a PR     controls       with its result
```

1. **Find.** `tradingagents strategy find "<query>"` calls data-mcp
   `paper_search` / `wiki_search` (1 credit each) and `paper_read` /
   `wiki_read` (free). Papers we find ourselves go through quant-mind's paper
   flow instead, which costs model tokens only.
2. **Extract.** One model call turns a paper into a **strategy card** (ours),
   with every field cited to a section:

   | Card field | Maps to |
   |---|---|
   | style, universe, horizon, benchmark | `Mandate.horizon_days`, `benchmark` |
   | thesis in one paragraph | `Mandate.thesis_frame` |
   | exclusion rules, each with the data it needs | `Mandate.disqualifiers` + screener tier 1/2 |
   | ranking signal, with its exact definition | a screener ordering signal |
   | what would make a call wrong | `Mandate.risk_frame`, kill criteria |
   | evidence claimed: period, universe, reported Sharpe/IC | review context only |

3. **Feasibility.** Code checks each required input against what our vendors
   can supply point-in-time. A card that needs data we do not have stops here,
   with the gap named.
4. **Human review.** A person turns the card into a mandate module or an
   overlay (`dataclasses.replace`, as `equity_momentum_leaps` does), or a new
   ordering signal in `screener/screen.py`. Nothing goes from a paper straight
   into the nightly job: auto-generated strategies that skip review are how
   overfitting gets paid for.
5. **Evaluate.** Historical screens at pre-registered dates -- the value
   screens' 2019-2024 half-year grid is the template -- each with its random
   control, adjudicated by the loop within the nightly budget.
6. **Promote or archive.** Promote when the edge over controls keeps its sign
   across screens with enough settled cells. `screen-review` reports the edge
   but sets no bar today, so L4 fixes one before the first candidate runs --
   a minimum number of screens and settled cells per side -- and applies it to
   hand-written mandates too. Otherwise file the card with its result in the
   knowledge layer: a strategy that failed here is knowledge too,
   and stops us testing it twice.

### Guarding against the intake itself

Testing many strategies and keeping the best one finds noise. The intake keeps a
trial count per style, screen dates are fixed before a candidate runs, and the
promotion bar rises with the number of candidates tried in that style. The
control group answers "is this better than picking at random from the eligible
pool", which is the question that matters and the one papers most often skip.

### Cost

Finding and extracting are cents: a few credits and one model call per paper.
Evaluation is the expensive part -- a 12-name screen is about 96 minutes of
model time -- so candidates queue into the nightly job's `--max-names` budget
rather than running as sweeps.

## Prompt content borrowed from `skills` (MIT)

No code, since there is none. Adapted text keeps LLMQuant's copyright notice in
a `NOTICE` entry.

- **Investor lenses** (`llmquant-investor-lenses`: Graham, Munger, Marks,
  Pabrai, Lynch and others) become `agent_guidance` for an overlay,
  `equity_value_lenses`. It is judged the way this framework judges anything:
  the same screens run under both mandates with `screen-run --as`, and the
  overlay is kept only if it beats plain `equity_value` against controls.
- **Strategy identities** (`llmquant-strategies`: long-biased, quant, equity
  long/short) as Portfolio Manager framing for mandates that match them.
- **Take-Profit Lab** (`llmquant-equities/workflows/take-profit-lab.md`): its
  exit-rule comparison and its "rollercoaster rate" -- the share of entries
  that reached a large gain and then gave most of it back -- define a study we
  can run now on our settled decisions, with no model calls. It measures
  whether a trailing or tiered exit would have beaten holding to the horizon.
- **Guardrails** copied into agent guidance where they fit: "do not invent
  scenario returns or factor exposures", "do not treat a simulation as a
  forecast", "do not overfit to the single best rule without showing
  alternatives".
- **Portfolio What-If Simulator / Exposure Map**: their output structure
  (current vs pro-forma exposure, concentration, liquidity) is the starting
  shape for the portfolio-construction design, which is out of scope here.

## Not used, and why

- **Magents code**: fills at the bar price with flat costs; a thin copy of
  ai-hedge-fund. Its one idea -- a capital pool split into strategy pods, each
  with its own drawdown kill-switch -- goes to the portfolio design as a risk
  budget per mandate.
- **data-mcp market data**: duplicates Alpha Vantage premium, which also covers
  institutional and ETF holdings (harvested above).
- **llmquant-book**: non-commercial licence.
- **quant-wiki in bulk**: reached through `wiki_search` when a question needs
  it, not mirrored.

## Outcomes we are after

| Outcome | Measured by | Today |
|---|---|---|
| Historical data fetched once | Requests to Alpha Vantage for final responses on a re-run of the same screen: target 0 | Every process refetches chains and statements |
| Faster screens | Wall time of a repeated `screen` + tier-2 pass | Tier 2 re-requests statements each run |
| Repeatable backtests | Share of cells whose numeric and text inputs are fully archived; a replay re-run reproduces the inputs exactly | 0%: "indicative rather than repeatable" |
| Fewer tokens per name | Provider-reported tokens per cell, before and after `search_knowledge` replaces bulk news | ~600k per name |
| Lessons used beyond the PM | Roles that receive point-in-time lessons | Portfolio Manager only |
| Prepared for future ideas | Datasets harvested with a point-in-time rule; weeks of holdings snapshots on disk; transcript backfill coverage | None harvested; holdings history cannot be recovered from the vendor |
| More strategies, honestly tested | Candidates taken through controlled screens; promoted vs archived, with trial counts | 3 mandates, all written by hand |
| Better calls | Edge over controls in `screen-review`, by mandate and overlay | +22.5% across 5 screens, most with 1-2 cells settled: not yet evidence |

The last row is what everything else is for, and it is the one no layer can
promise. The layers make runs cheaper and repeatable, so more screens settle
sooner; the controls then say whether any of it helps.

## Phases

| Phase | Scope | Done when |
|---|---|---|
| L1 | Data store: schema, `_make_api_request` wrapper, EDGAR and Yahoo adapters, freshness policy, replay mode, retire `av_daily` | A second run of the same historical screen makes no requests for final responses; a backtest cell passes in replay mode offline |
| H | Harvest: `snapshot` table, throttle, `tradingagents harvest` with the six datasets, graph filled from them, the four point-in-time tools | Holdings snapshots running weekly; insider and congressional trades refreshed; transcript backfill progressing within budget; a historical query returns only rows public by its date |
| L2 | Cheap wins on existing data: the exit-rule study (Take-Profit Lab), the `equity_value_lenses` overlay | Exit-rule table over settled decisions; lens overlay queued against the value screens |
| L3 | Knowledge layer: `[knowledge]` extra, adapter, ingest of news / reports / lessons, `search_knowledge`, archive-backed news in historical runs | Re-running a cell gives identical text inputs; tokens per cell measured before and after |
| L4 | Strategy intake: `strategy find`, card schema, feasibility check, trial log | The first external strategy is taken through controlled screens and promoted or archived |

L1, H and L2 need nothing from LLMQuant. H should follow L1 immediately:
holdings history starts on the day it runs. L3 is the quant-mind dependency. L4 is
the only phase that needs an LLMQuant API key.

## Risks and open questions

- **quant-mind churn.** It is pre-1.0, and its own docs mark shapes as
  "subject to change". The pin and the single adapter package contain this.
  If it stalls, the adapter's surface -- put a card, search with cutoffs -- is
  small enough to reimplement on SQLite FTS5.
- **LLMQuant pricing after the beta** is unknown. Only L4 depends on it, and
  papers found elsewhere work without it.
- **Alpha Vantage terms on retaining data.** Check the premium terms permit a
  persistent local store for personal research before L1 ships.
- **Look-ahead in the knowledge layer.** `available_at` must be publication
  time (news), filing time (EDGAR) or resolution time (lessons), never fetch
  time or analysis time. Test it the way `get_past_context` is tested: a
  historical search must not return a card that became available after the
  analysis date.
- **Disk.** The cache is 703 MB today (364 MB `av_daily`, 195 MB checkpoints,
  136 MB EDGAR). The transcript backfill (up to ~2.6 GB) and option chains are
  the largest new items; they are stored
  gzipped, and the nightly log prints the store's size so growth is visible. Pruning applies to
  non-final rows only.
- **Intake overfitting**: covered above; the trial count is the control.
- **Entity resolution.** Insider (`"KRISHNA, ARVIND"`) and institution
  (`"VANGUARD GROUP INC"`) names are free text. A normaliser keyed on the
  cleaned name plus ticker is enough for v1; mistakes split one node in two and
  never join two different ones.
- **Corrections after the fact.** Congressional filings carry `filing_status`
  (amendments), and 13Fs are amended. Store every version; a historical query
  sees the version public by its date.
- **Sector classification is not point-in-time.** `OVERVIEW` gives today's
  sector. Edges from it are dated at fetch, and a historical query that needs
  a past sector says the classification may postdate it.
- **Transcript coverage is uneven** for small and young listings. The harvest
  records what was asked and found nothing, so it is not asked again every run.

## Pointers for later designs (from awesome-trading-agents)

Portfolio construction and execution are separate designs. The strongest
starting points found:

- tradermonty/claude-trading-skills: `position-sizer` (with scripts),
  `exposure-coach`, `drawdown-circuit-breaker`, `pre-trade-discipline-gate`.
- JoelLewis/finance_skills: `bet-sizing`, `rebalancing`, `forward-risk`,
  `trade-execution`, `order-lifecycle`.
- flash131307/multi-agent-investment: agents gather evidence, a deterministic
  layer decides -- the pattern for sizing (the model proposes, code sizes).
- alpacahq/alpaca-mcp-server: paper and live orders for equities and options;
  the cheapest route to paper execution.
