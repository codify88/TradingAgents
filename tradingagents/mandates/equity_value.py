"""Value / quality-compounder mandate (Buffett-Munger framing)."""

from __future__ import annotations

from .analysts.value import VALUE_ANALYSTS
from .base import Mandate

EQUITY_VALUE = Mandate(
    name="equity_value",
    label="Long-Term Equity - Value / Quality",
    description=(
        "You are underwriting a durable business at a sensible price, expecting "
        "to hold it for years. You are buying a share of future owner earnings, "
        "not a ticker you intend to flip. Price volatility between now and the "
        "horizon is noise unless it changes what the business is worth."
    ),
    asset_class="equity",
    # Two years primary, with interim checkpoints so the memory log still
    # produces signal long before the thesis fully plays out.
    horizon_days=504,
    review_horizons_days=(63, 126, 252),
    # None -> config benchmark_map, which keeps upstream's per-exchange
    # benchmarks (SPY for US, ^N225 for .T, ...). Set a style index such as
    # IWD/VTV here instead to grade selection skill within the value factor
    # rather than total value-add over the market.
    benchmark=None,
    thesis_frame=(
        "that the business will still be earning attractive returns on capital "
        "in ten years, why competitors cannot take those returns away, that "
        "management allocates capital rationally, and that the current price "
        "leaves a margin of safety against a conservative estimate of intrinsic "
        "value. A cheap multiple alone is not a thesis; a great business at any "
        "price is not one either."
    ),
    analyst_guidance={
        "fundamentals": (
            "The Quality Analyst and Valuation Analyst receive a decade of "
            "computed metrics -- returns on capital, margins, cash conversion, "
            "capital allocation, multiples against history. Do not re-derive "
            "those ratios or growth rates yourself, and never quote a "
            "historical multiple or 'normal range' from memory. Your job is "
            "the recent record they cannot see in annual tables: what changed "
            "in the latest quarters and why, segment and business-mix shifts, "
            "one-off items (settlements, deposits, earn-outs, impairments, tax "
            "charges) and management's stated explanation for each, debt "
            "maturities, and any accounting-policy change. A single strong or "
            "weak quarter is not evidence of a trend -- say which it is."
        ),
        "market": (
            "Price action is secondary here and must not drive the "
            "recommendation. Use it only to (a) establish the current price "
            "and a sane entry range, (b) characterise the multi-year price "
            "trend -- valuation multiples belong to the Valuation Analyst -- and "
            "(c) flag whether recent weakness reflects a changed business or a "
            "changed mood. "
            "Do not present short-term overbought/oversold readings as reasons "
            "to buy or sell a multi-year position."
        ),
        "news": (
            "Distinguish durable changes to earning power from noise. A "
            "regulatory regime shift, a lost anchor customer, a technology "
            "substitution, or a capital-allocation blunder matters. A quarterly "
            "miss, an analyst downgrade, or a macro headline usually does not. "
            "State explicitly which bucket each item falls in."
        ),
        "social": (
            "Sentiment is a contrary input at this horizon, not a confirming "
            "one. Widespread enthusiasm raises the price you must pay; "
            "widespread disgust is where mispricing lives. Report what the "
            "crowd believes and, separately, whether the financial evidence "
            "supports it."
        ),
    },
    rating_guidance=(
        "Buy requires both a durable business and a meaningful discount to a "
        "conservative intrinsic value estimate. Overweight fits a good business "
        "at a fair price. Hold is the right answer for a fine business trading "
        "near fair value -- and is not a failure to decide. Underweight and "
        "Sell belong where the business has deteriorated or the price has run "
        "far past any defensible value, not merely where the chart looks tired."
    ),
    disqualifiers=(
        "The business is not understandable from the available evidence -- you "
        "cannot describe in plain language how it earns money and why that "
        "persists.",
        "Returns on invested capital have been structurally poor or "
        "deteriorating with no credible, evidenced explanation.",
        "Leverage is high enough that an ordinary downturn threatens solvency "
        "or forces dilution.",
        "Accounting quality is in doubt -- persistent divergence between "
        "reported earnings and cash generation.",
        "No margin of safety: the price already embeds an optimistic case.",
    ),
    indicator_shortlist=("close_200_sma", "close_50_sma", "atr"),
    risk_frame=(
        "risk is permanent loss of capital, not volatility. A deep drawdown in a "
        "business whose earning power is intact is an opportunity; a flat price "
        "on a business whose returns on capital are eroding is a loss not yet "
        "marked. Weigh two things: the chance that intrinsic value itself falls "
        "(moat erosion, leverage, capital misallocation, accounting that flatters "
        "earnings), and overpayment -- how far the price sits above a conservative "
        "value, because that gap is lost when the market stops paying for "
        "optimism. Exits are triggered by named breaks in the thesis, not by "
        "price stops; if you do name a price level, justify it as the point at "
        "which the price itself says the thesis has broken, not as a volatility "
        "band. Size to the probability and depth of permanent loss, not to "
        "recent price swings."
    ),
    analysts=VALUE_ANALYSTS,
)
