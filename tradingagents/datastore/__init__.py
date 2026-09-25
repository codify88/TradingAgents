"""Point-in-time store of vendor responses, with a shared request throttle.

``through_store`` is the one entry point the vendor clients use: it serves a
fresh stored response, or fetches (under the throttle), stores and returns.

Modes (``data_store`` config, or ``TRADINGAGENTS_DATA_STORE``, which wins and is
read at call time so a test or a one-off command can switch it):

- ``read_write`` (default): serve fresh rows, fetch and store the rest;
- ``replay``: never fetch; a miss raises ``ReplayMissError``. A backtest cell
  run this way provably used nothing new;
- ``off``: no store, no throttle -- the behaviour before the store existed.

Inside ``prefer_stored()`` any stored row is served whatever its age: for the
screen lab, which replays past dates and needs history, not today's copy.

Design: docs/design/llmquant.md, layer 1.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from .policy import NEW_YORK, is_fresh, policy_for
from .store import DataStore, params_key
from .throttle import acquire

__all__ = ["DataStore", "ReplayMissError", "get_store", "has_fresh", "prefer_stored", "store_mode",
           "through_store"]

_PREFER_STORED = contextvars.ContextVar("prefer_stored", default=False)


@contextlib.contextmanager
def prefer_stored() -> Iterator[None]:
    """Serve any stored row, however old; fetch only what was never stored."""
    token = _PREFER_STORED.set(True)
    try:
        yield
    finally:
        _PREFER_STORED.reset(token)

MODES = ("read_write", "replay", "off")

_stores: dict[Path, DataStore] = {}
_stores_lock = threading.Lock()


class ReplayMissError(RuntimeError):
    """Replay mode asked for a response the store does not hold."""


def _config() -> dict:
    from tradingagents.dataflows.config import get_config

    return get_config()


def store_mode(config: dict | None = None) -> str:
    mode = os.environ.get("TRADINGAGENTS_DATA_STORE") or (config or _config()).get("data_store", "read_write")
    if mode not in MODES:
        raise ValueError(f"data_store must be one of {MODES}, got {mode!r}")
    return mode


def store_path(config: dict | None = None) -> Path:
    config = config or _config()
    return Path(config.get("data_store_path")
                or Path(config["data_cache_dir"]) / "store.sqlite")


def get_store(config: dict | None = None) -> DataStore:
    path = store_path(config)
    with _stores_lock:
        if path not in _stores:
            _stores[path] = DataStore(path)
        return _stores[path]


def _is_error_body(body: str) -> bool:
    """A vendor error ("Error Message": invalid symbol, bad call). Never stored:
    Alpha Vantage sometimes answers a valid symbol this way and is fine seconds
    later, and callers retry on it -- a stored error would answer the retry.
    Callers' own in-process caches keep a persistent one from being re-asked."""
    if not body.lstrip().startswith("{"):
        return False
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and "Error Message" in parsed


def through_store(vendor: str, endpoint: str, params: dict, fetch: Callable[[], str], *,
                  per_minute: float | None = None) -> str:
    config = _config()
    mode = store_mode(config)
    if mode == "off":
        return fetch()

    store = get_store(config)
    now = datetime.now(UTC)
    day = now.astimezone(NEW_YORK).date().isoformat()
    key = params_key(params)
    policy = policy_for(endpoint, params, now)

    row = store.get(vendor, endpoint, key)
    # A row stored as final is served forever; anything else by the current policy.
    if row is not None and (row[2] or _PREFER_STORED.get() or is_fresh(policy, row[1], now)):
        store.count(vendor, endpoint, "hit", day)
        return row[0]
    if mode == "replay":
        raise ReplayMissError(f"{vendor} {endpoint} {key} is not in the store (replay mode)")

    if per_minute is None:
        per_minute = float(config.get("av_requests_per_minute", 150))
    acquire(store._conn(), vendor, per_minute)
    body = fetch()
    store.count(vendor, endpoint, "fetch", day)

    if _is_error_body(body):
        return body
    symbol = params.get("symbol") or params.get("tickers")
    store.put(vendor, endpoint, key, body, symbol=symbol, final=policy.final, fetched_at=now)
    if policy.snapshot:
        store.snapshot(vendor, endpoint, key, body, symbol=symbol, fetched_on=day)
    return body


def has_fresh(vendor: str, endpoint: str, params: dict) -> bool:
    """Whether a request would be served from the store without a fetch."""
    config = _config()
    if store_mode(config) == "off":
        return False
    row = get_store(config).get(vendor, endpoint, params_key(params))
    now = datetime.now(UTC)
    return row is not None and (row[2] or _PREFER_STORED.get()
                                or is_fresh(policy_for(endpoint, params, now), row[1], now))
