"""LLM-facing tools for the equity_value mandate's analysts.

Each tool returns compact markdown: tables of computed figures, the screens
they settle, and the definitions needed to read them. The analysts' prompts
tell them to cite these figures and not to recompute them.

A tool never raises into the graph. When data is missing -- no Alpha Vantage
key, a rate limit, a ticker the vendor does not cover -- it returns an
explicit UNAVAILABLE notice telling the analyst to report the gap rather than
fill it from memory. That second half matters: the P1 KO value run, lacking
these tools, asserted a "normal 20-22x" P/E band for KO; the computed ten-year
range is 23x to 32x.
"""

from __future__ import annotations

import math
from typing import Annotated

import pandas as pd
from langchain_core.tools import tool

from . import valuation as val
from .financials import Financials, load_financials, price_history
from .quality import _pct, annual_metrics, cagr, quality_screens, trend_per_year
from .render import guarded as _guarded, screens_block, table as _table

HISTORY_YEARS = 10
QUARTERS_SHOWN = 8


# --- formatting ---------------------------------------------------------------


def _money(x: float) -> str:
    if x is None or math.isnan(x):
        return "n/a"
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1e9:
        return f"{sign}{x / 1e9:,.2f}B"
    if x >= 1e6:
        return f"{sign}{x / 1e6:,.0f}M"
    return f"{sign}{x:,.0f}"


def _x(x: float, digits: int = 1) -> str:
    return "n/a" if x is None or math.isnan(x) else f"{x:.{digits}f}x"


def _header(title: str, f: Financials) -> str:
    latest_q = f.quarterly.index[-1].date() if not f.quarterly.empty else "n/a"
    return (
        f"## {title}: {f.ticker}\n"
        f"As of {f.as_of.date()} (point-in-time: only filings reported by this date). "
        f"Currency {f.currency}. Source: Alpha Vantage statements, "
        f"{len(f.annual)} fiscal years ({f.annual.index[0].year}-{f.annual.index[-1].year}); "
        f"latest quarter {latest_q}."
    )


def _screens_block(screens) -> str:
    return screens_block(screens, "equity_value")


_CITE = (
    "Cite these figures as given. Do not recompute ratios or substitute figures "
    "from memory; where a value is n/a, report the gap."
)


def _prices(f: Financials) -> pd.Series:
    start = f.annual.index[0] - pd.Timedelta(days=10)
    return price_history(f.ticker, start, f.as_of)


# --- quality ----------------------------------------------------------------------


@_guarded
def _quality_report(ticker: str, curr_date: str) -> str:
    f = load_financials(ticker, curr_date)
    m = annual_metrics(f)
    recent = m.iloc[-HISTORY_YEARS:]
    operating_income = f.col("operatingIncome")

    rows = [[
        str(end.year), _money(r.revenue), _pct(r.gross_margin), _pct(r.operating_margin),
        _pct(r.roic), _pct(r.rotc), _pct(r.roe), _money(r.fcf), _x(r.fcf_conversion, 2),
        f"{r.shares / 1e9:.3f}B" if not math.isnan(r.shares) else "n/a",
    ] for end, r in recent.iterrows()]
    annual = _table(
        ["FY", "Revenue", "Gross margin", "Op margin", "ROIC", "ROTC", "ROE", "FCF", "FCF/NI", "Shares"],
        rows,
    )

    roic = recent["roic"].dropna()
    worst_om = recent["operating_margin"].idxmin() if recent["operating_margin"].notna().any() else None
    slope = trend_per_year(recent["roic"])
    summary = [
        f"- Revenue CAGR: 5y {_pct(cagr(m['revenue'], 5))}, 10y {_pct(cagr(m['revenue'], 10))}. "
        f"Operating income CAGR: 5y {_pct(cagr(operating_income, 5))}, 10y {_pct(cagr(operating_income, 10))}.",
        f"- ROIC over {len(recent)}y: median {_pct(roic.median())}, low {_pct(roic.min())} "
        f"({roic.idxmin().year if not roic.empty else 'n/a'}), high {_pct(roic.max())}, "
        f"trend {'n/a' if math.isnan(slope) else f'{slope * 100:+.2f}pp/yr'}. "
        f"ROTC median {_pct(recent['rotc'].median())}.",
        f"- Gross margin range {_pct(recent['gross_margin'].min())}-{_pct(recent['gross_margin'].max())}; "
        f"operating margin median {_pct(recent['operating_margin'].median())}, worst "
        f"{_pct(recent['operating_margin'].min())} ({worst_om.year if worst_om is not None else 'n/a'}).",
        f"- Share count CAGR: 5y {_pct(cagr(m['shares'], 5), 2)}, 10y {_pct(cagr(m['shares'], 10), 2)} "
        f"(negative = shrinking share count).",
        f"- Latest fiscal year balance sheet: net debt {_money(m['net_debt'].iloc[-1])}, "
        f"net debt/EBITDA {_x(m['net_debt_to_ebitda'].iloc[-1])}, "
        f"interest coverage {_x(m['interest_coverage'].iloc[-1])}.",
    ]

    q_ocf = f.col("operatingCashflow", quarterly=True)
    q_capex = f.col("capitalExpenditures", quarterly=True).abs()
    q_div = f.col("dividendPayout", ("dividendPayoutCommonStock",), quarterly=True).abs()
    q_ni = f.col("netIncome", quarterly=True)
    q_rows = [[
        end.strftime("%Y-%m"), _money(q_ocf.get(end)), _money(q_capex.get(end)),
        _money(q_ocf.get(end) - q_capex.get(end)), _money(q_div.get(end)), _money(q_ni.get(end)),
    ] for end in f.quarterly.index[-QUARTERS_SHOWN:]]
    ttm_conv = f.ttm("operatingCashflow") / f.ttm("netIncome") if f.ttm("netIncome") > 0 else float("nan")
    quarterly = _table(["Quarter", "Op cash flow", "Capex", "FCF", "Dividends", "Net income"], q_rows)

    return "\n\n".join([
        _header("Business quality", f),
        f"### Annual history (last {len(recent)} fiscal years)\n{annual}",
        "### Summary\n" + "\n".join(summary),
        f"### Quarterly cash flow (last {len(q_rows)} quarters)\n{quarterly}\n"
        f"Trailing-twelve-month operating cash flow / net income: {_x(ttm_conv, 2)}. "
        f"A single quarter far out of line with the rest is usually a one-off "
        f"(settlement, deposit, earn-out) -- identify it before treating a weak "
        f"fiscal year as a trend.",
        _screens_block(quality_screens(m, HISTORY_YEARS, ttm_conv)),
        "### Definitions\n"
        "ROIC = operating income x (1 - effective tax rate) / average (debt + equity - cash); "
        "it includes goodwill and acquired intangibles, so it is the return on what management "
        "paid. ROTC excludes them: the return on the capital the business needs to operate, "
        "the moat measure. A wide ROTC-ROIC gap means acquisitions earn much less than the core. "
        "Operating income is used, not Alpha Vantage's `ebit`, which includes non-operating gains. "
        "FCF = operating cash flow - capital expenditure.\n" + _CITE,
    ])


@_guarded
def _capital_allocation_report(ticker: str, curr_date: str) -> str:
    f = load_financials(ticker, curr_date)
    m = annual_metrics(f).iloc[-HISTORY_YEARS:]

    total_ocf = m["ocf"].sum(min_count=1)
    uses = [
        ("Capital expenditure", m["capex"].sum(min_count=1)),
        ("Dividends", m["dividends"].sum(min_count=1)),
        ("Buybacks", m["buybacks"].sum(min_count=1)),
        ("Acquisitions and investments (net)", m["acquisitions_and_investments"].sum(min_count=1)),
    ]
    use_rows = [[name, _money(v), _pct(v / total_ocf) if total_ocf and not math.isnan(total_ocf) else "n/a"]
                for name, v in uses]
    net_debt_change = m["net_debt"].iloc[-1] - m["net_debt"].iloc[0]
    goodwill_change = m["goodwill"].iloc[-1] - m["goodwill"].iloc[0]

    rows = [[
        str(end.year), _money(r.ocf), _money(r.capex), _money(r.dividends), _money(r.buybacks),
        _money(r.acquisitions_and_investments), _money(r.fcf - r.dividends),
        _x(r.fcf / r.dividends, 2) if r.dividends and not math.isnan(r.dividends) else "n/a",
        _money(r.net_debt),
    ] for end, r in m.iterrows()]

    first, last = m.index[0].year, m.index[-1].year
    return "\n\n".join([
        _header("Capital allocation", f),
        f"### Where the operating cash went, FY{first}-FY{last}\n"
        f"Cumulative operating cash flow: {_money(total_ocf)}.\n"
        + _table(["Use", "Cumulative", "% of operating cash flow"], use_rows)
        + f"\nNet debt changed by {_money(net_debt_change)} over the period "
          f"({_money(m['net_debt'].iloc[0])} -> {_money(m['net_debt'].iloc[-1])}); "
          f"goodwill changed by {_money(goodwill_change)} (the balance-sheet footprint of acquisitions).",
        "### By year\n" + _table(
            ["FY", "Op cash flow", "Capex", "Dividends", "Buybacks", "Acq/invest", "FCF - dividends",
             "FCF/dividends", "Net debt"],
            rows,
        ),
        f"Share count CAGR over the period: {_pct(cagr(m['shares'], len(m) - 1), 2)} "
        f"(negative = buybacks outpacing issuance).",
        "Read this as an owner: were buybacks made at sensible prices, were acquisitions "
        "paid for from cash flow or from debt, and is the dividend covered by free cash "
        "flow through the cycle or funded by borrowing? " + _CITE,
    ])


# --- valuation ------------------------------------------------------------------


@_guarded
def _valuation_report(ticker: str, curr_date: str) -> str:
    f = load_financials(ticker, curr_date)
    prices = _prices(f)
    snap = val.snapshot(f, prices)
    if snap is None:
        raise RuntimeError(f"no price for {f.ticker} within a week of {f.as_of.date()}")
    hist = val.historical_multiples(f, prices).iloc[-HISTORY_YEARS:]

    current = [
        f"- Price {snap.price:.2f} on {snap.price_date.date()}; shares {snap.shares / 1e9:.3f}B; "
        f"market cap {_money(snap.market_cap)}; debt {_money(snap.debt)}; cash {_money(snap.cash)}; "
        f"enterprise value {_money(snap.enterprise_value)}.",
        f"- Trailing twelve months: revenue {_money(snap.ttm_revenue)}, operating income "
        f"{_money(snap.ttm_operating_income)}, net income {_money(snap.ttm_net_income)}, FCF "
        f"{_money(snap.ttm_fcf)}, dividends {_money(snap.ttm_dividends)}.",
        f"- Normalized FCF ({val.NORMALIZATION_YEARS}y median): {_money(snap.normalized_fcf)}.",
    ]

    def compare(name, now, series, fmt):
        s = series.dropna()
        if s.empty:
            return [name, fmt(now), "n/a", "n/a", "n/a"]
        pct = val.percentile_in_history(now, s)
        return [name, fmt(now), fmt(s.median()), f"{fmt(s.min())} - {fmt(s.max())}",
                "n/a" if math.isnan(pct) else f"{pct * 100:.0f}th"]

    vs_history = _table(
        ["Multiple", "Now", f"{len(hist)}y median", "Range", "Percentile vs own history"],
        [
            compare("EV / EBIT", snap.ev_to_ebit, hist["ev_to_ebit"], _x),
            compare("P / E", snap.pe, hist["pe"], _x),
            compare("FCF yield (TTM)", snap.fcf_yield, hist["fcf_yield"], _pct),
            compare("FCF yield (normalized)", snap.normalized_fcf_yield, hist["fcf_yield"], _pct),
            compare("Dividend yield", snap.dividend_yield, hist["dividend_yield"], _pct),
        ],
    )
    hist_rows = [[
        str(end.year), f"{r.price:.2f}", _x(r.ev_to_ebit), _x(r.pe), _pct(r.fcf_yield), _pct(r.dividend_yield),
    ] for end, r in hist.iterrows()]

    return "\n\n".join([
        _header("Valuation vs own history", f),
        "### Current\n" + "\n".join(current),
        "### Now vs the company's own history\n" + vs_history
        + "\nFor multiples (EV/EBIT, P/E) a high percentile means expensive relative to "
          "the company's own past; for yields a high percentile means cheap.",
        "### At each fiscal year end\n" + _table(
            ["FY", "Price", "EV/EBIT", "P/E", "FCF yield", "Dividend yield"], hist_rows),
        "EBIT is operating income. Historical multiples use the close on the fiscal "
        "year-end date and that year's statements. " + _CITE,
    ])


@_guarded
def _reverse_dcf_report(ticker: str, curr_date: str) -> str:
    f = load_financials(ticker, curr_date)
    snap = val.snapshot(f, _prices(f))
    if snap is None:
        raise RuntimeError(f"no price for {f.ticker} within a week of {f.as_of.date()}")

    bases = {"normalized": snap.normalized_fcf, "trailing": snap.ttm_fcf}
    grid_rows = []
    for label, base in bases.items():
        grid_rows.append([f"{label} ({_money(base)})"] + [
            _pct(val.reverse_dcf(snap.market_cap, base, r)) for r in val.DISCOUNT_RATE_GRID
        ])
    implied_grid = _table(
        ["FCF base"] + [f"r = {_pct(r, 0)}" for r in val.DISCOUNT_RATE_GRID], grid_rows)

    m = annual_metrics(f)
    oi = f.col("operatingIncome")
    record_rows = [
        ["Revenue", _pct(cagr(m["revenue"], 5)), _pct(cagr(m["revenue"], 10))],
        ["Operating income", _pct(cagr(oi, 5)), _pct(cagr(oi, 10))],
        ["Free cash flow", _pct(cagr(m["fcf"], 5)), _pct(cagr(m["fcf"], 10))],
    ]

    def value_grid(base: float) -> str:
        rows = []
        for g in (0.0, 0.03, 0.06):
            cells = []
            for r in val.DISCOUNT_RATE_GRID:
                if math.isnan(base) or base <= 0:
                    cells.append("n/a")
                    continue
                per_share = val.dcf_value(base, g, r) / snap.shares
                mos = 1 - snap.price / per_share
                cells.append(f"{per_share:.2f} ({_pct(mos, 0)})")
            rows.append([f"{_pct(g, 0)}/yr"] + cells)
        return _table(["FCF growth for 10y"] + [f"r = {_pct(r, 0)}" for r in val.DISCOUNT_RATE_GRID], rows)

    record, record_label = val.growth_record(f)
    implied_screen = {k: val.reverse_dcf(snap.market_cap, b, val.SCREEN_DISCOUNT_RATE) for k, b in bases.items()}
    screen = val.margin_of_safety_screen(implied_screen, record, record_label)

    return "\n\n".join([
        _header("Reverse DCF: what the price implies", f),
        f"Market cap {_money(snap.market_cap)} at {snap.price:.2f}. Equity free cash flow is "
        f"discounted over {val.DEFAULT_YEARS} years, then a terminal value growing "
        f"{_pct(val.DEFAULT_TERMINAL_GROWTH)} a year.",
        "### FCF growth per year the price requires\n" + implied_grid
        + "\nRead across: the implied rate moves several points per point of discount rate, "
          "so treat the grid, not one cell, as the answer.",
        "### What the business has delivered\n" + _table(["Series", "5y CAGR", "10y CAGR"], record_rows)
        + "\nThe screen below measures the implied rate against the best profit series "
          "(operating income or free cash flow), not revenue: the price is paying for cash "
          "flow, and revenue that outgrew profit did not deliver it.",
        "### Value per share (margin of safety vs current price)\n"
        f"On normalized FCF ({_money(snap.normalized_fcf)}):\n" + value_grid(snap.normalized_fcf)
        + f"\n\nOn trailing FCF ({_money(snap.ttm_fcf)}):\n" + value_grid(snap.ttm_fcf)
        + "\nA negative margin of safety means the price is above that value.",
        _screens_block([screen]),
        "The normalized base is the median of the last five fiscal years; the trailing "
        "base is the last four quarters. When they disagree, the question is whether "
        "the recent years were depressed or inflated by something that will not recur -- "
        "answer it from the quarterly cash-flow evidence. " + _CITE,
    ])


# --- tool objects -------------------------------------------------------------------


@tool
def get_quality_metrics(
    ticker: Annotated[str, "ticker symbol"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
) -> str:
    """Ten years of computed business-quality metrics: margins, ROIC, return on
    tangible capital, ROE, free-cash-flow conversion, share count, leverage, the
    last eight quarters of cash flow, and the equity_value quality screens."""
    return _quality_report(ticker, curr_date)


@tool
def get_capital_allocation(
    ticker: Annotated[str, "ticker symbol"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
) -> str:
    """Where ten years of operating cash flow went: capex, dividends, buybacks,
    acquisitions, and debt, with dividend coverage by free cash flow per year."""
    return _capital_allocation_report(ticker, curr_date)


@tool
def get_valuation_history(
    ticker: Annotated[str, "ticker symbol"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
) -> str:
    """Current EV/EBIT, P/E, FCF and dividend yield, set against the same
    multiples at each of the company's last ten fiscal year ends."""
    return _valuation_report(ticker, curr_date)


@tool
def get_reverse_dcf(
    ticker: Annotated[str, "ticker symbol"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
) -> str:
    """The free-cash-flow growth the current price implies across discount
    rates, the company's actual growth record, a value-per-share grid, and the
    margin-of-safety screen."""
    return _reverse_dcf_report(ticker, curr_date)


QUALITY_TOOLS = (get_quality_metrics, get_capital_allocation)
VALUATION_TOOLS = (get_valuation_history, get_reverse_dcf)
