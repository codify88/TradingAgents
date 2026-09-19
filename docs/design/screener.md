# The screener

## What it is for

The agent loop is an adjudication tool, not a search tool. One run costs roughly
thirty LLM calls and six to eight minutes, and there are ~8,600 active US common
stocks. Running the loop across the universe would be about six weeks of
continuous compute. Something cheap has to decide what gets adjudicated.

## Tiers

| Tier | Narrows | Costs |
|---|---|---|
| universe | ~14,400 listings → ~5,700 common stocks | one `LISTING_STATUS` call |
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

`tradingagents screen-run <id>` (or `screen --run`) adjudicates tier 3: picks and
control through the loop as one sweep under the screen's mandate and date, with
a run id derived from the screen's so it resumes. `screen-run --all` runs every
unfinished screen and is safe to schedule.

`tradingagents screen-review` joins saved manifests back to the decision log and
reports picks against controls. It refuses a verdict until both arms settle:
picks returning well while no control has settled is a statement about the
market over that window, not about the screen.

## Common shares only

About one Alpha Vantage "Stock" row in seven is a warrant, right, unit, note or
preferred. Dashed symbols were always dropped; the undashed ones are now dropped
by NASDAQ's convention (a listed symbol plus W/R/U/Z/P/O/N/M, or P plus one of
those: ZION+O, AEP+PZ) or by the filed name. Checked against the full listing
for false positives: class shares (GOOGL), "Preferred Bank" and a royalty
trust's units (MARPS) are kept.

Prices come from Alpha Vantage, one request per name, not from Yahoo's bulk
download. Yahoo throttled the bulk download by silently dropping symbols,
differently each run: the same 2025-09-02 momentum screen passed 630 names
through the price tier once and 650 the next time, with nothing else running,
and a 2024-03-01 value screen lost 35 of its 60 finalists to "Too Many
Requests". Alpha Vantage answers the same way every time and raises on a
failure rather than returning nothing, so a vendor problem is counted as
unavailable instead of read as "no price history". The cost is time -- ~5,300
requests, about 27 minutes at the screen's pace -- so each day's answers are
kept on disk (`<data_cache_dir>/av_daily/`, purged after three days) and a
rerun, or another date's screen, pays only for what is missing. The ten-year
price history behind the value ordering reads the same source, split-adjusted
but not dividend-adjusted, as a market capitalisation needs.

## Survivorship

A screen dated in the past has to see the companies that have since died, or its
history is a study of survivors. Four places could drop them:

| Stage | Problem | Now |
|---|---|---|
| universe | today's listings exclude everything delisted since | listings as of the screen date (`LISTING_STATUS date=`); gone-since names are marked |
| price | Yahoo drops a ticker's history when it delists | prices come from Alpha Vantage, which keeps it |
| fundamentals | Alpha Vantage returns empty statements for delisted companies | read from SEC EDGAR; what EDGAR cannot answer (IFRS filers, ambiguous names) is excluded with that reason, and counted |
| grading | no prices, so the decision never settles | Alpha Vantage prices; a name delisted mid-horizon settles at its last trade |

Fundamentals for a delisted company come from SEC EDGAR (`mandates/tools/edgar.py`).
EDGAR maps only current tickers, so the company's name -- from Alpha Vantage's
delisted listing, with dates so a reused ticker resolves correctly -- is matched
against every name that has ever filed, and only a single filer that was filing
10-Ks or 10-Qs around the screen date is accepted; anything else is no data,
never a guess. Facts are admitted from the day they were filed, restatements
included, and year-to-date quarterly cash flows are converted to single quarters.
Validated against Coca-Cola, where both sources exist: margins, ROE, FCF
conversion and interest coverage match exactly, ROIC within half a point. A
2022-03-01 value screen over 400 names lost none of its 14 delisted candidates to
missing statements.

Still unrecoverable: IFRS filers (no US-GAAP facts), names that match no single
filer, and analyst estimate revisions, which are not filed. Every historical
screen counts what remains in its Survivorship note.

## When not to believe it

A vendor outage and a dead company look identical in a dict of price frames.
yfinance throttling once returned empty for every symbol, and an earlier version
reported that as 6,052 companies having no price history while still emitting a
confident shortlist from what leaked through. A vendor failure is now retried
and then reported separately from "no price history"; above 5% of the universe
unreachable the run withholds the shortlist entirely and spends no fundamentals
budget. (The bar was 25% while the price tier was Yahoo, where some loss was
routine; at 5% a few missing names already changed which six were picked.)

Alpha Vantage's measured ceiling on the key this was built against is ~257
requests per minute. The fundamentals tier is paced below that and retries a
throttled call, because "unavailable" in a report reads as a fact about the
company rather than as the screener having asked too fast.
