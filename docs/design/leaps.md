# LEAPS: expressing an equity call through long-dated options

Status: design, not built. Probed 2026-09-19.

## The question

A LEAPS mandate should answer one thing the equity mandates cannot: **when an
equity thesis is right, is a long-dated call a better way to hold it than the
stock?** A call caps the loss at the premium and levers the gain, but it costs
time value, forgoes dividends, and pays the spread twice. Whether that trade is
worth it depends on the contract's price, not on the business -- so it is a
separate judgement from "is this a good stock", and it can be graded separately.

## Data (probed)

Alpha Vantage `HISTORICAL_OPTIONS` (premium) returns the full chain for a symbol
on a date: contract id, expiry, strike, type, bid/ask with sizes, last, mark,
volume, open interest, implied volatility and all five greeks. Chains go back
to at least 2012 and include every listed expiry, so LEAPS are point-in-time
testable.

| Probe | LEAPS expiries (>= 1y out) | LEAPS calls | Vendor IV < 5% | No two-sided quote | Median spread |
|---|---|---|---|---|---|
| KO 2016-06-01 | 2018-01 | 14 | 6 | 0 | 4.7% |
| AAPL 2019-06-03 | 2020-06 to 2021-06 (4) | 173 | 35 | 3 | 10.9% |
| META 2022-12-01 | 2024-01 to 2025-01 (3) | 224 | 0 | 0 | 6.0% |
| KO 2023-03-01 | 2024-06, 2025-01 | 45 | 20 | 1 | 5.6% |
| JNJ 2023-09-01 | 2025-01, 2025-06 | 60 | 28 | 0 | 7.7% |
| KO 2024-03-01 | 2025-06, 2026-01 | 43 | 25 | 1 | 5.5% |
| INTC 2024-03-01 | 2025-09 to 2026-12 (5) | 85 | 5 | 0 | 8.4% |
| NVDA 2025-09-02 | 2026-09 to 2027-12 (5) | 449 | 0 | 0 | 1.5% |

What this settles:

1. **Quotes are usable, vendor IV and greeks are not.** Bid/ask is two-sided on
   almost every contract, but on low-volatility names 20-60% of LEAPS calls
   report an IV under 5% with delta 1.00 near the money (KO 2024-03-01: a
   strike-55 call on a ~$60 stock at IV 1.5%). We compute IV and delta
   ourselves from the bid/ask mid with Black-Scholes-Merton, using the
   Treasury yield (`TREASURY_YIELD`) and the trailing dividend yield
   (`DIVIDENDS`, point-in-time by ex-date). `last` is stale and never used.
2. **Spreads are a first-class cost.** 1.5-11% of the premium, paid on entry and
   on exit. Grading buys at the ask and sells at the bid.
3. **Strikes are in the raw, unsplit terms of the date** (KO 2012 strikes 60-70
   on a ~$69 pre-split stock). Pricing and grading use raw closes, which the
   Alpha Vantage daily frame already carries.
4. **Expiry supply limits the horizon.** The longest listed expiry is usually
   the January about two years out, and sometimes only ~1.6 years (KO
   2016-06-01). A 126-day momentum hold always fits; a 504-day value hold often
   does not.

## Proposed shape: an overlay, not a new strategy

LEAPS reuses an equity mandate's whole analysis and adds one decision at the
end. The equity mandate (value or momentum) still decides direction and
conviction; a **LEAPS Analyst** then answers "stock or call, and is the call
cheap enough?"; the Portfolio Manager rates as before and additionally chooses
the instrument.

- **Contract selection is a rule, not a judgement.** A tool picks the contract:
  the shortest expiry at least the holding horizon plus 63 trading days out,
  and the strike nearest a target delta (0.75 by default -- stock replacement,
  most of the stock's move for about half its capital). The agents judge
  whether that contract is worth buying; they never pick strikes, so every
  graded LEAPS call is reproducible.
- **Calls only in v1.** Buy or Overweight may be expressed as a call; Hold,
  Underweight and Sell mean no position. Puts, spreads and hedges are later.

### LEAPS Analyst tools and screens (computed)

`get_leaps_candidates(ticker, date)` -- the selected contract and its
neighbours: mid, spread %, our IV and delta, open interest, extrinsic value per
year, dividend forgone, break-even move by the horizon, and leverage (stock
return multiple per 1% move).

| Screen | TRIPPED when | Why |
|---|---|---|
| Illiquid | Spread above 10% of mid, or open interest under 100 | The round trip eats the edge |
| Expensive volatility | Our IV above 1.3x 1-year realised volatility | You are paying for moves the stock has not been making |
| Time value too costly | Break-even move by the horizon exceeds the thesis's own expected move | The call loses even if the stock thesis is right |
| No expiry long enough | Nothing listed at horizon + 63 trading days | Would force a roll, which v1 does not model |

Thresholds are starting points to be set by backtest, not fitted to it.

### Grading

Each LEAPS decision is graded three ways over the mandate horizon:

1. **Option return**: bought at the ask on the analysis date, sold at the bid on
   the exit date from that day's chain (same contract id), or at intrinsic value
   if held to expiry.
2. **Against the stock**: option return minus the stock's total return --
   positive means the instrument choice added value.
3. **Against a matched stock position**: the stock position with the same
   dollar risk (delta x shares), which separates skill in choosing the
   instrument from simply taking more leverage.

The conviction check extends naturally: did the calls the agents chose beat the
stock more often than calls they rejected would have?

## Open decisions

1. **Overlay or standalone?** Recommended: overlay on an equity mandate
   (`--mandate equity_momentum --instrument leaps`), because it reuses the
   analysis and isolates the instrument decision for grading.
2. **Which horizon?** Momentum's 126 days fits every chain. Value's 504 days
   needs a roll or a shorter LEAPS horizon (252 days is a natural fit).
3. **Target delta.** 0.75 (stock replacement) by default; 0.5 (at the money)
   is a more leveraged, cheaper, more convex bet.

## Build order

1. `dataflows`: point-in-time chain fetch (cached per symbol and date), our own
   IV and delta, risk-free rate and dividend yield. Unit-tested against
   textbook Black-Scholes values.
2. `mandates/tools/leaps.py`: contract selection and the four screens.
3. LEAPS Analyst and the instrument field on the Portfolio Manager's decision.
4. Grading: option P&L from historical chains at exit, the two comparisons,
   and the conviction extension.
5. A sweep on the momentum cells already run, so the instrument choice is
   graded against theses whose stock outcome is already known.
