# Fixing the Hold habit: a playbook

## The problem

The agents rarely rate Buy. Across production runs, value rated 1% Buy, 15% Overweight, 74% Hold;
momentum 7% Overweight, 40% Underweight. The first two live nights of the standard strategy rated
ten names: six Holds, four Underweights. The paper book buys only Buy and Overweight, so it has
never traded.

## Why (the three suspects)

1. **The rating scale is written for someone who owns the stock.** The Portfolio Manager reads
   "Hold: maintain current position, no action needed" and "Sell: exit position or avoid entry".
   With no position, "no action" is always safe. The Research Manager's scale has the same wording.
2. **Every agent is told not to assume a flat book.** The portfolio context says "do not assume a
   flat book; give direction and sizing guidance in terms the caller can apply to their own
   position", which is position-management language.
3. **Nothing puts a price on Hold.** No base rate, no probability, no cost of standing aside. And the
   Conservative risk analyst argues general caution, whatever the evidence.

## The tools

**Knobs** (Agents → Knobs). Each knob is one change. Its first option is production, so leaving a
knob alone changes nothing. Pick options, name the combination, and save it as a variant.

| Knob | Options | What it changes |
|---|---|---|
| Rating scale | position · **entry** · **relative** | The PM's and Research Manager's scale: an entry decision for a flat book, or a forecast against the benchmark |
| Holdings | unknown · **flat** | Every agent is told the book is flat, with cash to deploy |
| What Hold takes | judgement · **probability** · no_hold | The PM states P(beats the benchmark) and rates by fixed bands, or cannot Hold (diagnostic only) |
| Base rate | none · **half** | The PM is told about half the names beat the benchmark |
| Conservative voice | general · **evidence** | The Conservative analyst argues only the evidenced downside |
| Holding period framing | on · off | Whether the 5-day holding period is stated (off is upstream) |
| Risk debate rounds | 1 · 2 | More back-and-forth in the risk debate |

**Replays** (Agents → Replays). These re-run only the last stages of saved decisions (192 of them,
182 with outcomes) under a variant. The analysts' reports and debates are reused, so a case costs
cents.

| Stage | Re-runs | Per case, Haiku | Per case, production models |
|---|---|---|---|
| `pm` | Portfolio Manager | ~$0.02 | ~$0.12 |
| `risk` | three risk analysts + PM | ~$0.10 | ~$0.30 |
| `trader` | trader + risk + PM | ~$0.12 | ~$0.33 |
| `research` | bull/bear debate + Research Manager + trader + risk + PM | ~$0.21 | ~$0.65 |

These are estimates. Once a stage has run, the form uses its measured token counts.

**Suites** (Agents → Suites / Runs). The full pipeline on fixed past screens. This is the final test
before adopting a variant, and it costs dollars per case.

## The loop

1. **Fix a case set and keep it.** Same mandate filter, count and seed for every replay you compare.
   A variant is compared with a `baseline` replay of the same cases, never with the original
   ratings: a replay has no past-decision lessons, and every sampling differs.
2. **Change one knob at a time** until you know what each one does.
3. **Combine the ones that helped**, and re-run on the same cases.
4. **Move up a stage.** A knob that touches the Research Manager (the scale) or the risk debate (the
   Conservative voice) only shows its full effect at `research` or `risk`.
5. **Confirm on a suite** with the production models before anything goes live.

## Suggested order

| # | Replay | Stage | Why |
|---|---|---|---|
| 0 | `baseline` | pm | The reference on your case set. Run it once per model. |
| 1 | scale = entry | pm | The main suspect. Does Hold fall and Buy/Overweight rise? |
| 2 | scale = relative | pm | The alternative framing. Compare with 1. |
| 3 | book = flat | pm | Suspect 2, on its own. |
| 4 | hold_bar = probability | pm | Adds calibration: are its 60% calls right 60% of the time? |
| 5 | hold_bar = no_hold | pm | Diagnostic: which way does the PM lean when it can't Hold? If it leans right (positive edge), the information is there and the scale is hiding it. |
| 6 | anchor = half | pm | Does a base rate undo the bearish lean? |
| 7 | best of 1–6 + caution = evidence | risk | Does the risk debate stop dragging calls to Hold? |
| 8 | best combination | research | The whole back half, with the Research Manager's scale changed too. |
| 9 | the winner | suite | The full pipeline, production models, before adoption. |

Start on Haiku with 40–60 cases ("any" mandate, seed 7): the whole pm-stage sequence costs a few
dollars. Then re-run the leaders on the production models (Opus 4.8 / Sonnet 5), because models
differ in exactly this habit.

## Reading a replay

- **Mix.** Buy / Overweight / Hold / Underweight / Sell, against baseline on the same cases. Bullish
  share and Hold share are the headline.
- **Moves.** The original → new rating table shows which calls a knob changed.
- **Edge.** Mean 5-day alpha of the Buy/Overweight calls minus the rest, with a t-stat. **This is
  what matters.** More Buys with no edge makes the book worse. Aim for a bullish share near a third
  or more, *with an edge that doesn't shrink*.
- **Hit rate.** Of the directional calls (not Hold), the share that went the called way.
- **Calibration** (probability knob). Brier score: 0.25 is a coin flip, lower is better. Mean
  probability near 0.5 means it isn't systematically bearish.

## Cautions

- **Samples are small.** 60 cases with 5-day alpha give a noisy edge; a t-stat under 2 is a hint,
  not a result. Look for the sign holding across models and case sets.
- **Mandates differ.** Value and momentum cases were decided under long mandate horizons; the replay
  scores everything on the horizon you choose (5 sessions by default, matching standard). The
  standard strategy's own cases ("none") grow by five a night.
- **The no-Hold knob is a probe, not a setting.**
- **Horizon.** Try 21 sessions too: a knob that only helps at 5 days may be noise.

## Adopting a winner

A variant that wins on a suite goes live as a production prompt change, behind a config switch so it
can be turned off. Ask Claude to wire it in: say which variant, and the replay and suite that
justified it.
