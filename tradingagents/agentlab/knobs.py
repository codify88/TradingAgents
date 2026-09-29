"""Knobs: named, combinable changes aimed at the agents' rating habit.

The agents rarely rate Buy. The final rating scale is written for someone who
already holds the stock ("Hold: maintain current position, no action needed";
"Sell: exit position or avoid entry"), the portfolio context tells every agent
not to assume a flat book, and nothing anchors what a Hold should cost. With
no position, "no action" is always the safe answer.

Each knob changes one of those things, and each option is a set of prompt edits
(applied by ``hook.py``) or settings. ``compose`` turns one option per knob into
an ordinary ``Variant``, recorded with the choices, so a knob-built variant runs
anywhere a hand-written one does: suites (the full pipeline) or replays (the
final stages on saved decisions, ``replay.py``). The first option of every knob
is production, so leaving a knob alone changes nothing.

``docs/design/rating-playbook.md`` is the order to turn them in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .variants import Edit, Variant

PM, RM, TRADER = "Portfolio Manager", "Research Manager", "Trader"
CONSERVATIVE, AGGRESSIVE, NEUTRAL = "Conservative Analyst", "Aggressive Analyst", "Neutral Analyst"

# Exact passages from the prompts (tests check each still occurs in its source).
PM_SCALE = """**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position, no action needed
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit position or avoid entry"""

RM_SCALE = """**Rating Scale** (use exactly one):
- **Buy**: Strong conviction in the bull thesis; recommend taking or growing the position
- **Overweight**: Constructive view; recommend gradually increasing exposure
- **Hold**: Balanced view; recommend maintaining the current position
- **Underweight**: Cautious view; recommend trimming exposure
- **Sell**: Strong conviction in the bear thesis; recommend exiting or avoiding the position"""

PORTFOLIO_UNKNOWN = ("Portfolio context: not provided. You do not know the caller's current "
                     "holdings or cash, so do not assume a flat book; give direction and "
                     "sizing guidance in terms the caller can apply to their own position.")

CONSERVATIVE_OPENING = ("your primary objective is to protect assets, minimize volatility, and ensure "
                        "steady, reliable growth. You prioritize stability, security, and risk mitigation, "
                        "carefully assessing potential losses, economic downturns, and market volatility.")

ENTRY_SCALE = """**Rating Scale** (use exactly one). The book holds no position in this name; the rating is what to do with new capital today:
- **Buy**: open a full position -- the evidence clearly favours it beating the benchmark over the holding period
- **Overweight**: open a half position -- it leans towards beating the benchmark
- **Hold**: do not open a position -- no edge either way
- **Underweight**: it leans towards lagging the benchmark -- avoid it
- **Sell**: the evidence clearly favours it lagging the benchmark"""

RELATIVE_SCALE = """**Rating Scale** (use exactly one): your forecast of this stock's return relative to the benchmark over the holding period, not advice about an existing position:
- **Buy**: expected to beat the benchmark by a clear margin
- **Overweight**: expected to beat the benchmark modestly
- **Hold**: expected to roughly match the benchmark
- **Underweight**: expected to lag the benchmark modestly
- **Sell**: expected to lag the benchmark by a clear margin"""

FLAT_BOOK = ("Portfolio context: the book holds no position in this name and has cash to deploy. "
             "Not buying is a decision too: it forgoes whatever the name returns over the holding period.")

PROBABILITY = ("Before choosing the rating, estimate the probability that this stock beats the benchmark "
               "over the holding period, and begin the Executive Summary with "
               "\"Probability of beating the benchmark: NN%.\" Then rate from it: 60% or more is Buy, "
               "53-59% Overweight, 47-52% Hold, 41-46% Underweight, 40% or less Sell.")

NO_HOLD = ("Hold is not available for this decision: rate the side the evidence leans to, however "
           "slightly, and let Overweight or Underweight carry a weak lean.")

BASE_RATE = ("Calibration: about half of the names that reach you beat the benchmark over any holding "
             "period, so across many decisions your bullish and bearish calls should be of similar number. "
             "A run of Holds and Underweights means the scale is being used as caution, not as a forecast.")

EVIDENCE_BOUND = ("your role is to name the specific, evidenced downside of this plan over the holding period. "
                  "Argue from the reports' facts, not from general caution: if the downside case is weak, "
                  "say so plainly, and do not oppose a position merely because it carries risk.")


@dataclass(frozen=True)
class Option:
    key: str
    label: str
    detail: str
    edits: tuple[Edit, ...] = ()
    settings: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Knob:
    key: str
    label: str
    question: str
    options: tuple[Option, ...]

    def option(self, key: str) -> Option:
        found = next((o for o in self.options if o.key == key), None)
        if found is None:
            raise ValueError(f"knob {self.key}: no option {key!r} ({', '.join(o.key for o in self.options)})")
        return found


KNOBS: tuple[Knob, ...] = (
    Knob("scale", "Rating scale", "What does each rating mean to the Portfolio Manager and Research Manager?", (
        Option("position", "Position management (production)",
               "Hold = keep what you have; Sell = exit or avoid. Written for a book that owns the stock."),
        Option("entry", "Entry decision",
               "The book is flat; the rating is what to do with new capital: full, half, or no position.",
               (Edit(PM, "replace", ENTRY_SCALE, PM_SCALE), Edit(RM, "replace", ENTRY_SCALE, RM_SCALE))),
        Option("relative", "Relative forecast",
               "The rating is a forecast of return against the benchmark over the holding period.",
               (Edit(PM, "replace", RELATIVE_SCALE, PM_SCALE), Edit(RM, "replace", RELATIVE_SCALE, RM_SCALE))),
    )),
    Knob("book", "Holdings", "What is every agent told about the portfolio?", (
        Option("unknown", "Unknown (production)", "Holdings not given; told not to assume a flat book."),
        Option("flat", "Flat book",
               "No position and cash to deploy; not buying forgoes the name's return.",
               (Edit("*", "replace", FLAT_BOOK, PORTFOLIO_UNKNOWN),)),
    )),
    Knob("hold_bar", "What Hold takes", "When may the final call be Hold?", (
        Option("judgement", "Judgement (production)", "Hold when the evidence is balanced or thin."),
        Option("probability", "State a probability",
               "The PM writes P(beats the benchmark) and rates by fixed bands; the lab scores its calibration.",
               (Edit(PM, "append", PROBABILITY),)),
        Option("no_hold", "No Hold (diagnostic)",
               "Forces a side. Not for production: it shows what the agents lean to when Hold is removed.",
               (Edit(PM, "append", NO_HOLD),)),
    )),
    Knob("anchor", "Base rate", "Is the PM told how often names beat the benchmark?", (
        Option("none", "None (production)", "No anchor."),
        Option("half", "About half",
               "Bullish and bearish calls should be of similar number over many decisions.",
               (Edit(PM, "append", BASE_RATE),)),
    )),
    Knob("caution", "Conservative voice", "How does the Conservative risk analyst argue?", (
        Option("general", "General caution (production)", "Protect assets, minimise volatility."),
        Option("evidence", "Evidence-bound",
               "Names the specific downside from the reports; says so when the downside case is weak.",
               (Edit(CONSERVATIVE, "replace", EVIDENCE_BOUND, CONSERVATIVE_OPENING),)),
    )),
    Knob("horizon", "Holding period framing", "Are the agents told the holding period they are graded on?", (
        Option("on", "Told (production)", "The 5-day holding period and benchmark are stated."),
        Option("off", "Not told (upstream)", "Upstream's prompts: no holding period stated.",
               settings={"holding_period_framing": False}),
    )),
    Knob("risk_rounds", "Risk debate rounds", "How many rounds do the three risk analysts argue?", (
        Option("1", "One (production)", "Each risk analyst speaks once."),
        Option("2", "Two", "Each speaks twice: more cost, more back-and-forth.",
               settings={"max_risk_discuss_rounds": 2}),
    )),
)

BY_KEY = {k.key: k for k in KNOBS}
PRODUCTION = {k.key: k.options[0].key for k in KNOBS}


def compose(name: str, choices: dict[str, str], description: str = "",
            models: dict[str, str] | None = None) -> Variant:
    """A variant from one option per knob (knobs left out stay at production)."""
    unknown = set(choices) - set(BY_KEY)
    if unknown:
        raise ValueError(f"unknown knob(s): {', '.join(sorted(unknown))}")
    picked = {**PRODUCTION, **choices}
    edits: list[Edit] = []
    settings: dict = {}
    for knob in KNOBS:
        opt = knob.option(picked[knob.key])
        edits += list(opt.edits)
        settings.update(opt.settings)
    changed = {k: v for k, v in picked.items() if v != PRODUCTION[k]}
    auto = ", ".join(f"{BY_KEY[k].label}: {BY_KEY[k].option(v).label}" for k, v in changed.items()) or "production"
    return Variant(name, description or f"Knobs -- {auto}", edits=edits, settings=settings,
                   models={k: v for k, v in (models or {}).items() if v}, knobs=changed)


def catalog() -> list[dict]:
    return [{"key": k.key, "label": k.label, "question": k.question,
             "options": [{"key": o.key, "label": o.label, "detail": o.detail,
                          "agents": sorted({e.agent for e in o.edits}),
                          "settings": o.settings} for o in k.options]} for k in KNOBS]
