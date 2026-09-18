# The screener

## What it is for

The agent loop is an adjudication tool, not a search tool. One run costs roughly
thirty LLM calls and six to eight minutes, and there are ~8,600 active US common
stocks. Running the loop across the universe would be about six weeks of
continuous compute. Something cheap has to decide what gets adjudicated.

## Tiers

| Tier | Narrows | Costs |
|---|---|---|
| universe | 8,590 → ~6,300 | one `LISTING_STATUS` call |
| price | ~6,300 → ~230 | one batched download per 200 names |
| liquidity budget | ~230 → ~60 | nothing; a neutral cut |
| fundamentals | ~60 → ~30 | a few Alpha Vantage calls per name |
| agent loop | ~30 → 5–10 | ~30 LLM calls each |

The cut into the fundamentals tier is made on **liquidity**, deliberately, so a
mandate's own signal is not applied twice — once to choose who gets examined and
again to rank the results.

## Two rules

**1. Exclusions narrow; the thesis does not.**

A screener that ranks on the same signals the analysts then weigh hands each
analyst a name pre-selected to look good on its own criteria, and every
candidate "confirms". So the narrowing is done by the mandates' own
disqualifiers, run in reverse, and only where the data says `TRIPPED`. A `WATCH`
is exactly the contestable call the analyst debate exists to make; `NO DATA` is
not evidence of anything. Neither excludes.

`equity_value` gets **no price-based thesis exclusion at all** — a cheap stock in
a downtrend is the mandate's whole point, and excluding on trend would mean the
names it exists to find never reach the analysts.

**2. Every run carries a random control.**

A few names are drawn at random from the eligible pool the ranking did *not*
pick — disjoint from the shortlist, seeded so they reproduce. Run them through
the loop alongside the picks. Without them there is no way to answer the only
question worth asking about a screener: does the ordering beat picking an
eligible name at random?

Ordering is one declared signal per mandate, named in the report:

| Mandate | Ordering |
|---|---|
| `equity_value` | FCF yield percentile against the company's own ten-year history |
| `equity_momentum` | 12-month excess total return over SPY |

One number, not a composite, so whatever bias it introduces is visible.

## Reviewing it

`tradingagents screen --mandate equity_value` prints the funnel, every drop
reason, the shortlist and the control, and writes a manifest under
`results_dir/screens/`. No LLM call, so a screen can be sanity-checked before
tier 3 is paid for.

`tradingagents screen-review` joins saved manifests back to the decision log and
reports picks against controls. It refuses a verdict until both arms settle:
picks returning well while no control has settled is a statement about the
market over that window, not about the screen.

## When not to believe it

A vendor outage and a dead company look identical in a dict of price frames.
yfinance throttling once returned empty for every symbol, and an earlier version
reported that as 6,052 companies having no price history while still emitting a
confident shortlist from what leaked through. A batch empty for *every* symbol is
now treated as a vendor failure, retried with backoff, and reported separately;
above 25% of the universe unreachable the run withholds the shortlist entirely
and spends no API budget.

Alpha Vantage's measured ceiling on the key this was built against is ~257
requests per minute. The fundamentals tier is paced below that and retries a
throttled call, because "unavailable" in a report reads as a fact about the
company rather than as the screener having asked too fast.
