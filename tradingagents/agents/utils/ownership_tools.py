"""Agent tools over the harvested ownership and earnings-call data.

Each answers as of the run's trade date and shows only what was public then
(``tradingagents.dataflows.ownership``). Registered but not yet in any analyst's
tool list: which role reads which is a mandate decision.
"""

from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from tradingagents.dataflows.interface import route_to_vendor


@tool
def get_congress_trades(
    ticker: Annotated[str, "ticker symbol"],
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """Stock trades by members of Congress in this company, dated by when they were disclosed."""
    return route_to_vendor("get_congress_trades", ticker, trade_date)


@tool
def get_institutional_holdings(
    ticker: Annotated[str, "ticker symbol"],
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """The largest institutional (13F) holders and how their positions changed."""
    return route_to_vendor("get_institutional_holdings", ticker, trade_date)


@tool
def get_etf_exposure(
    ticker: Annotated[str, "ticker symbol"],
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """ETFs that hold this company, by weight."""
    return route_to_vendor("get_etf_exposure", ticker, trade_date)


@tool
def get_earnings_call(
    ticker: Annotated[str, "ticker symbol"],
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """The company's most recent earnings call before the trade date: who spoke and what they said."""
    return route_to_vendor("get_earnings_call", ticker, trade_date)
