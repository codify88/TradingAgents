"""The broker: Alpaca's REST API, paper endpoint by default.

A small adapter rather than the SDK -- five calls are all the book needs, and
each is easy to fake in a test. Keys come from the environment
(``ALPACA_API_KEY``, ``ALPACA_SECRET_KEY``); the live endpoint is refused
unless ``trading_live`` is set in the config, which nothing sets yet.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
TIMEOUT = 20


class BrokerError(RuntimeError):
    pass


@dataclass
class Account:
    equity: float
    cash: float
    buying_power: float
    long_market_value: float
    blocked: bool


@dataclass
class Position:
    symbol: str
    qty: float
    market_value: float


@dataclass
class Order:
    client_order_id: str
    symbol: str
    side: str
    qty: float
    status: str          # new | accepted | filled | partially_filled | canceled | rejected | expired ...
    filled_qty: float
    filled_avg_price: float | None


class Broker(Protocol):
    paper: bool

    def account(self) -> Account: ...
    def positions(self) -> list[Position]: ...
    def submit(self, symbol: str, qty: int, side: str, client_order_id: str) -> Order: ...
    def orders(self, after: str) -> list[Order]: ...
    def cancel_open(self) -> int: ...
    def sessions(self, start: str, end: str) -> list[str]: ...


def _order(d: dict) -> Order:
    return Order(
        client_order_id=d.get("client_order_id", ""), symbol=d["symbol"], side=d["side"],
        qty=float(d.get("qty") or 0), status=d["status"], filled_qty=float(d.get("filled_qty") or 0),
        filled_avg_price=float(d["filled_avg_price"]) if d.get("filled_avg_price") else None)


class AlpacaBroker:
    def __init__(self, config: dict | None = None, session=None):
        config = config or {}
        self.key = os.getenv("ALPACA_API_KEY")
        self.secret = os.getenv("ALPACA_SECRET_KEY")
        if not self.key or not self.secret:
            raise BrokerError("ALPACA_API_KEY and ALPACA_SECRET_KEY are not set (.env)")
        self.paper = not config.get("trading_live", False)
        self.base = PAPER_URL if self.paper else LIVE_URL
        if session is None:
            import requests

            session = requests.Session()
        self.http = session
        self.http.headers.update({"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret})

    def _call(self, method: str, path: str, **kw):
        r = self.http.request(method, f"{self.base}{path}", timeout=TIMEOUT, **kw)
        if r.status_code >= 400:
            raise BrokerError(f"{method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else None

    def account(self) -> Account:
        a = self._call("GET", "/v2/account")
        return Account(float(a["equity"]), float(a["cash"]), float(a["buying_power"]),
                       float(a.get("long_market_value") or 0),
                       bool(a.get("trading_blocked") or a.get("account_blocked")))

    def positions(self) -> list[Position]:
        return [Position(p["symbol"], float(p["qty"]), float(p["market_value"]))
                for p in self._call("GET", "/v2/positions")]

    def submit(self, symbol: str, qty: int, side: str, client_order_id: str) -> Order:
        return _order(self._call("POST", "/v2/orders", json={
            "symbol": symbol, "qty": str(int(qty)), "side": side, "type": "market",
            "time_in_force": "opg", "client_order_id": client_order_id}))

    def orders(self, after: str) -> list[Order]:
        return [_order(o) for o in self._call(
            "GET", "/v2/orders", params={"status": "all", "after": after, "limit": 500, "direction": "asc"})]

    def cancel_open(self) -> int:
        out = self._call("DELETE", "/v2/orders") or []
        return len(out)

    def sessions(self, start: str, end: str) -> list[str]:
        return [d["date"] for d in self._call("GET", "/v2/calendar", params={"start": start, "end": end})]
