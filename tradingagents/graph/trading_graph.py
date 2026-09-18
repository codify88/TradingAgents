# TradingAgents/graph/trading_graph.py

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf
from langgraph.prebuilt import ToolNode

# Import the abstract tool methods from agent_utils
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_global_news,
    get_income_statement,
    get_indicators,
    get_insider_transactions,
    get_macro_indicators,
    get_news,
    get_prediction_markets,
    get_stock_data,
    get_verified_market_snapshot,
    resolve_instrument_identity,
)
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.utils import get_current_date, safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients import create_llm_client
from tradingagents.mandates import get_mandate, render_mandate_context
from tradingagents.reporting import write_report_tree

from .checkpointer import checkpoint_step, clear_checkpoint, get_checkpointer, thread_id
from .conditional_logic import ConditionalLogic
from .propagation import Propagator
from .reflection import Reflector
from .setup import GraphSetup
from .signal_processing import SignalProcessor

logger = logging.getLogger(__name__)


def _validate_trade_date(trade_date) -> str:
    """The run date as a canonical ``YYYY-MM-DD`` string no later than today."""
    value = str(trade_date)
    try:
        canonical = datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value
    except ValueError:
        canonical = False
    if not canonical:
        raise ValueError(f"trade_date must be a date in YYYY-MM-DD format, got {trade_date!r}")
    if value > get_current_date():
        raise ValueError(f"trade_date cannot be in the future: {value}")
    return value


def _coerce_max_retries(value):
    """Validate an ``llm_max_retries`` value to a non-negative int.

    Accepts an int or a numeric string (env vars arrive as strings). Rejects
    booleans and negatives loudly so a misconfiguration fails at startup rather
    than silently disabling retries.
    """
    if isinstance(value, bool):
        raise ValueError(f"llm_max_retries must be an integer, not a boolean: {value!r}")
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"llm_max_retries must be an integer, got {value!r}") from exc
    if n < 0:
        raise ValueError(f"llm_max_retries must be >= 0, got {n}")
    return n


def _coerce_max_tokens(value):
    """Validate a ``max_tokens`` value to a positive int (env vars are strings)."""
    if isinstance(value, bool):
        raise ValueError(f"max_tokens must be an integer, not a boolean: {value!r}")
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"max_tokens must be an integer, got {value!r}") from exc
    if n <= 0:
        raise ValueError(f"max_tokens must be > 0, got {n}")
    return n


def _tz_naive(frame):
    """Drop the timezone from a price frame's index, so vendors compare cleanly."""
    if getattr(frame.index, "tz", None) is not None:
        frame = frame.copy()
        frame.index = frame.index.tz_localize(None)
    return frame


class TradingAgentsGraph:
    """Main class that orchestrates the trading agents framework."""

    # Declared at class level, not only assigned in __init__, so that callers
    # which build a bare instance (``object.__new__``) or a ``MagicMock(spec=...)``
    # still see them. The defaults are the "no mandate" case, which reproduces
    # upstream behaviour exactly.
    mandate_name: str = ""
    mandate = None
    mandate_context: str = ""

    # Fallback grading window when neither a mandate nor config says otherwise.
    DEFAULT_HOLDING_DAYS = 5

    def __init__(
        self,
        selected_analysts=("market", "social", "news", "fundamentals"),
        debug=False,
        config: dict[str, Any] = None,
        callbacks: list | None = None,
        mandate: str | None = None,
    ):
        """Initialize the trading agents graph and components.

        Args:
            selected_analysts: List of analyst types to include
            debug: Whether to run in debug mode
            config: Configuration dictionary. If None, uses default config
            callbacks: Optional list of callback handlers (e.g., for tracking LLM/tool stats)
        """
        self.debug = debug
        self.config = config or DEFAULT_CONFIG
        self.callbacks = callbacks or []

        # Resolve the mandate once config is available, so an explicit argument
        # wins over the config/env default. An unknown name raises here.
        self.mandate_name = (
            mandate if mandate is not None else self.config.get("mandate", "")
        ) or ""
        self.mandate = get_mandate(self.mandate_name)
        self.mandate_context = render_mandate_context(self.mandate)

        # Update the interface's config
        set_config(self.config)

        # Create necessary directories
        os.makedirs(self.config["data_cache_dir"], exist_ok=True)
        os.makedirs(self.config["results_dir"], exist_ok=True)

        # Initialize LLMs with provider-specific thinking configuration
        llm_kwargs = self._get_provider_kwargs()

        # Add callbacks to kwargs if provided (passed to LLM constructor)
        if self.callbacks:
            llm_kwargs["callbacks"] = self.callbacks

        deep_client = create_llm_client(
            provider=self.config["llm_provider"],
            model=self.config["deep_think_llm"],
            base_url=self.config.get("backend_url"),
            **llm_kwargs,
        )
        quick_client = create_llm_client(
            provider=self.config["llm_provider"],
            model=self.config["quick_think_llm"],
            base_url=self.config.get("backend_url"),
            **llm_kwargs,
        )

        self.deep_thinking_llm = deep_client.get_llm()
        self.quick_thinking_llm = quick_client.get_llm()

        self.memory_log = TradingMemoryLog(self.config)

        # Create tool nodes
        self.tool_nodes = self._create_tool_nodes()

        # Initialize components
        self.conditional_logic = ConditionalLogic(
            max_debate_rounds=self.config["max_debate_rounds"],
            max_risk_discuss_rounds=self.config["max_risk_discuss_rounds"],
        )
        self.graph_setup = GraphSetup(
            self.quick_thinking_llm,
            self.deep_thinking_llm,
            self.tool_nodes,
            self.conditional_logic,
        )

        self.propagator = Propagator(
            max_recur_limit=self.config.get("max_recur_limit", 100),
        )
        self.reflector = Reflector(self.quick_thinking_llm)
        self.signal_processor = SignalProcessor(self.quick_thinking_llm)

        # State tracking
        self.curr_state = None
        self.ticker = None
        self.log_states_dict = {}  # date to full state dict

        # Graph-shape-affecting run choices, kept for the checkpoint signature.
        self.selected_analysts = tuple(selected_analysts)

        # Set up the graph: keep the workflow for recompilation with a checkpointer.
        self.workflow = self.graph_setup.setup_graph(
            selected_analysts,
            mandate_analysts=self.mandate.analysts if self.mandate is not None else (),
        )
        self.graph = self.workflow.compile()
        self._checkpointer_ctx = None
        self._resuming = False

    def _get_provider_kwargs(self) -> dict[str, Any]:
        """Get provider-specific kwargs for LLM client creation."""
        kwargs = {}
        provider = self.config.get("llm_provider", "").lower()

        if provider == "google":
            thinking_level = self.config.get("google_thinking_level")
            if thinking_level:
                kwargs["thinking_level"] = thinking_level

        elif provider == "openai":
            reasoning_effort = self.config.get("openai_reasoning_effort")
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort

        elif provider == "anthropic":
            effort = self.config.get("anthropic_effort")
            if effort:
                kwargs["effort"] = effort

        # Sampling temperature is cross-provider: forward it whenever set.
        # float() here so a value coming from a TRADINGAGENTS_TEMPERATURE env
        # string ("0.2") works the same as a programmatic float.
        temperature = self.config.get("temperature")
        if temperature is not None and temperature != "":
            kwargs["temperature"] = float(temperature)

        # SDK retry budget is cross-provider. Forward it only when explicitly set
        # so each provider keeps its own default (usually 2) otherwise (#1091).
        max_retries = self.config.get("llm_max_retries")
        if max_retries is not None and max_retries != "":
            kwargs["max_retries"] = _coerce_max_retries(max_retries)

        # Output-token cap is cross-provider, but Gemini names it
        # ``max_output_tokens``; forward under the right key when set (#1204).
        max_tokens = self.config.get("max_tokens")
        if max_tokens is not None and max_tokens != "":
            key = "max_output_tokens" if provider == "google" else "max_tokens"
            kwargs[key] = _coerce_max_tokens(max_tokens)

        return kwargs

    def _create_tool_nodes(self) -> dict[str, ToolNode]:
        """Create tool nodes for different data sources using abstract methods."""
        return {
            "market": ToolNode(
                [
                    # Core stock data tools
                    get_stock_data,
                    # Technical indicators
                    get_indicators,
                    # Deterministic verification snapshot (bound to the analyst
                    # LLM and required by its prompt; must be executable here or
                    # the call fails and the model reports it "unavailable").
                    get_verified_market_snapshot,
                ]
            ),
            "social": ToolNode(
                [
                    # News tools for social media analysis
                    get_news,
                ]
            ),
            "news": ToolNode(
                [
                    # News and insider information
                    get_news,
                    get_global_news,
                    get_insider_transactions,
                    get_macro_indicators,
                    get_prediction_markets,
                ]
            ),
            "fundamentals": ToolNode(
                [
                    # Fundamental analysis tools
                    get_fundamentals,
                    get_balance_sheet,
                    get_cashflow,
                    get_income_statement,
                ]
            ),
        }

    def _resolve_benchmark(self, ticker: str) -> str:
        """Pick the benchmark ticker for alpha calculation against ``ticker``.

        ``config["benchmark_ticker"]`` overrides everything when set; otherwise
        the suffix map matches the ticker's exchange suffix (e.g. ``.T`` for
        Tokyo). US-listed tickers without a dotted suffix fall through to the
        empty-suffix entry (SPY by default). Unrecognised suffixes (including
        US tickers with dots like ``BRK.B``) also fall back to the empty-suffix
        entry, which is the right default because the alpha calculation works
        in USD.
        """
        from tradingagents.dataflows.symbol_utils import normalize_symbol

        explicit = self.config.get("benchmark_ticker")
        if explicit:
            # Same alias mapping as the analyzed ticker; an unmapped alias finds
            # no prices, and the decision would stay pending for good.
            return normalize_symbol(explicit)
        # A mandate may name its own benchmark (e.g. a momentum sleeve graded
        # against MTUM). An explicit config value still wins, since that is the
        # user deliberately overriding everything.
        if self.mandate is not None and self.mandate.benchmark:
            return normalize_symbol(self.mandate.benchmark)
        benchmark_map = self.config.get("benchmark_map", {})
        ticker_upper = normalize_symbol(ticker)
        for suffix, benchmark in benchmark_map.items():
            if suffix and ticker_upper.endswith(suffix.upper()):
                return benchmark
        return benchmark_map.get("", "SPY")

    def _default_holding_days(self) -> int:
        """Grading window for a decision made under no mandate.

        Upstream made this configurable in v0.5.0; a mandate overrides it with
        its own horizon, so the two layer rather than compete.
        """
        return int(self.config.get("holding_period_days", self.DEFAULT_HOLDING_DAYS))

    def _mandate_for(self, mandate_name: str = ""):
        """The mandate a decision should be graded under, or None.

        Resolution order: the mandate the decision was *made* under (recorded on
        its memory-log entry), then this graph's mandate, then nothing. The
        decision's own mandate wins even when the current run uses a different
        one, so a two-year value thesis is never re-graded as a momentum trade.
        """
        for name in (mandate_name, self.mandate_name):
            if not name:
                continue
            try:
                mandate = get_mandate(name)
            except ValueError:
                continue  # a log entry from a mandate this build no longer knows
            if mandate is not None:
                return mandate
        return None

    def _holding_days_for(self, mandate_name: str = "") -> int:
        """Trading days to grade a decision over.

        Falls back to upstream's 5-day default with no mandate. Grading a
        two-year value thesis on a five-day return is the single most damaging
        default for long-horizon work -- it teaches the memory log that patience
        is a mistake.
        """
        mandate = self._mandate_for(mandate_name)
        return mandate.horizon_days if mandate is not None else self._default_holding_days()

    def _horizons_for(self, mandate_name: str = "") -> tuple[int, ...]:
        """Every horizon this decision is graded at, earliest first.

        The last element is the primary horizon that settles the entry; the ones
        before it are interim review checkpoints. Without a mandate there is a
        single horizon, which is exactly upstream's behaviour.
        """
        mandate = self._mandate_for(mandate_name)
        if mandate is None:
            return (self._default_holding_days(),)
        return mandate.all_horizons_days

    @staticmethod
    def _calendar_span(trading_days: int) -> int:
        """Calendar days to request to be sure ``trading_days`` bars have printed.

        252 trading days is about 365 calendar days, so a fixed weekend buffer
        is only ever right for very short windows: asking for 504 trading days
        over 511 calendar days returns barely two thirds of the window, and the
        entry would stay pending forever. Scale, then add slack for holidays.
        """
        return int(trading_days * 1.5) + 10

    # A stock whose last bar is this many calendar days older than the
    # benchmark's has stopped trading. Two weeks cannot be a late print: a
    # vendor that is merely behind is behind on the benchmark too.
    DELISTED_GAP_DAYS = 14

    @staticmethod
    def _returns_at_horizons(
        ticker: str, trade_date: str, horizons, benchmark: str = "SPY",
    ) -> dict:
        """Raw/alpha return and resolution date at each horizon that has settled.

        One price download covers every horizon, so checking three interim
        checkpoints costs the same two requests as checking one. Horizons whose
        full window has not traded yet are simply absent from the result (#1169),
        as are all of them when the symbol is unreachable.

        Delisted names are graded, not abandoned. Yahoo drops a ticker's history
        when it delists, so the series comes from Alpha Vantage instead; and a
        name that stopped trading inside the horizon -- acquired, taken private,
        bankrupt -- settles at its last trade once the benchmark shows the
        horizon has passed, with that last trade's date as the resolution date.
        Without both, every decision on a company that later disappeared would
        stay pending forever, and any aggregate over the log would be computed on
        survivors alone.
        """
        from tradingagents.dataflows.symbol_utils import normalize_symbol

        horizons = sorted({int(h) for h in horizons})
        if not horizons:
            return {}
        try:
            start = datetime.strptime(trade_date, "%Y-%m-%d")
            end = start + timedelta(days=TradingAgentsGraph._calendar_span(horizons[-1]))
            end_str = end.strftime("%Y-%m-%d")

            # Normalize so the realized-return lookup hits the same instrument
            # the analysis priced (e.g. XAUUSD -> GC=F) (#984). The benchmark is
            # already a canonical Yahoo symbol from ``_resolve_benchmark``.
            stock = yf.Ticker(normalize_symbol(ticker)).history(start=trade_date, end=end_str)
            bench = yf.Ticker(benchmark).history(start=trade_date, end=end_str)
            if stock.empty:
                from tradingagents.mandates.tools.financials import alpha_vantage_daily

                full = alpha_vantage_daily(ticker)
                if not full.empty:
                    stock = full[(full.index >= pd.Timestamp(trade_date))
                                 & (full.index < pd.Timestamp(end_str))]
            stock, bench = _tz_naive(stock), _tz_naive(bench)
            delisted = (
                not stock.empty and not bench.empty
                and (bench.index[-1] - stock.index[-1]).days
                >= TradingAgentsGraph.DELISTED_GAP_DAYS
            )

            settled = {}
            for days in horizons:
                # Require the full window in both series. A rerun before it has
                # traded leaves the horizon unsettled to retry next run, rather
                # than settling on a premature partial return (#1169). The one
                # exception is a stock that has stopped trading: its window will
                # never fill, so once the benchmark's has, it settles at its last
                # trade.
                if len(bench) <= days:
                    continue
                if len(stock) <= days:
                    if delisted:
                        last = stock.index[-1]
                        raw = float(stock["Close"].iloc[-1] / stock["Close"].iloc[0] - 1)
                        bench_at = bench["Close"][bench.index <= last]
                        bench_ret = float(bench_at.iloc[-1] / bench["Close"].iloc[0] - 1)
                        settled[days] = (raw, raw - bench_ret, last.strftime("%Y-%m-%d"))
                    continue
                raw = float(
                    (stock["Close"].iloc[days] - stock["Close"].iloc[0])
                    / stock["Close"].iloc[0]
                )
                bench_ret = float(
                    (bench["Close"].iloc[days] - bench["Close"].iloc[0])
                    / bench["Close"].iloc[0]
                )
                # The date of the last price bar used is when this outcome became
                # known — the point-in-time cutoff for injecting the lesson (#1251).
                settled[days] = (
                    raw, raw - bench_ret, stock.index[days].strftime("%Y-%m-%d"),
                )
            return settled
        except Exception as e:
            logger.warning(
                "Could not resolve outcome for %s on %s vs %s (will retry next run): %s",
                ticker, trade_date, benchmark, e,
            )
            return {}

    def _fetch_returns(
        self, ticker: str, trade_date: str, holding_days: int | None = None,
        benchmark: str = "SPY",
    ) -> tuple[float | None, float | None, int | None, str | None]:
        """Fetch raw and alpha return for ticker over holding_days from trade_date.

        ``holding_days`` defaults to the active mandate's horizon (see
        :meth:`_holding_days_for`), falling back to upstream's 5 days.

        ``benchmark`` is the index used as the alpha baseline (resolved by the
        caller via ``_resolve_benchmark``). Returns ``(raw_return, alpha_return,
        holding_days, resolution_date)`` — where ``resolution_date`` is the date
        of the last price bar used, i.e. when the outcome became known (#1251) —
        or ``(None, None, None, None)`` when the outcome cannot be settled yet:
        the full holding window has not traded (#1169), or the symbol is delisted
        or unreachable.
        """
        if holding_days is None:
            holding_days = self._holding_days_for()
        # Called unbound in places, so reach the primitive through the class.
        settled = TradingAgentsGraph._returns_at_horizons(
            ticker, trade_date, (holding_days,), benchmark,
        )
        if holding_days not in settled:
            return None, None, None, None
        raw, alpha, resolution_date = settled[holding_days]
        return raw, alpha, holding_days, resolution_date

    def _resolve_pending_entries(self, ticker: str) -> None:
        """Settle or checkpoint pending log entries for ticker at the start of a run.

        Two outcomes per entry are possible. If the primary horizon has fully
        traded the entry is settled as before: reflection written, tag resolved.
        If it has not, any interim review horizon that *has* come due is recorded
        as a checkpoint on the still-pending entry. That is what gives a
        long-horizon mandate a learning signal before it settles -- an
        equity_value call would otherwise teach nothing for two years.

        Writes are batched: all reviews in one atomic write, all settlements in
        another. Entries whose price data is not yet available are left alone.

        Trade-off: only same-ticker entries are resolved per run.  Entries for
        other tickers accumulate until that ticker is run again.
        """
        pending = [e for e in self.memory_log.get_pending_entries() if e["ticker"] == ticker]
        if not pending:
            return

        benchmark = self._resolve_benchmark(ticker)
        updates, reviews = [], []
        for entry in pending:
            mandate_name = entry.get("mandate", "")
            primary = self._holding_days_for(mandate_name)
            raw, alpha, days, resolution_date = self._fetch_returns(
                ticker, entry["date"], benchmark=benchmark, holding_days=primary,
            )
            if raw is not None:
                try:
                    reflection = self.reflector.reflect_on_final_decision(
                        final_decision=entry.get("decision", ""),
                        raw_return=raw,
                        alpha_return=alpha,
                        benchmark_name=benchmark,
                        holding_days=days,
                    )
                except Exception as exc:
                    # Reflection calls a provider, and this runs on the way into
                    # a new run: a transient failure leaves the entry pending for
                    # the next one rather than stopping the analysis that was
                    # asked for.
                    logger.warning(
                        "Reflection failed for %s on %s: %s", ticker, entry["date"], exc
                    )
                    continue
                updates.append({
                    "ticker": ticker,
                    "trade_date": entry["date"],
                    "mandate": mandate_name,
                    "raw_return": raw,
                    "alpha_return": alpha,
                    "holding_days": days,
                    "reflection": reflection,
                    "resolution_date": resolution_date,
                })
                # Interim checkpoints are moot once the final outcome is known:
                # the reflection above carries the lesson, and back-filling
                # superseded checkpoints would only spend LLM calls.
                continue
            reviews.extend(
                self._due_reviews(entry, ticker, primary, benchmark)
            )

        if reviews:
            self.memory_log.batch_append_reviews(reviews)
        if updates:
            self.memory_log.batch_update_with_outcomes(updates)

    def _due_reviews(
        self, entry: dict, ticker: str, primary: int, benchmark: str,
    ) -> list[dict]:
        """Interim checkpoints that have come due on one unsettled entry.

        Skips horizons already recorded, so reviews are written once and a rerun
        is free. Returns [] for a mandate that declares no review horizons --
        including the no-mandate case -- which keeps an upstream run on exactly
        its old code path, with no extra price requests.
        """
        horizons = self._horizons_for(entry.get("mandate", ""))
        already = {r["days"] for r in entry.get("reviews", ())}
        candidates = [h for h in horizons[:-1] if h not in already]
        if not candidates:
            return []

        settled = self._returns_at_horizons(
            ticker, entry["date"], candidates, benchmark,
        )
        due = []
        for days in sorted(settled):
            raw, alpha, resolution_date = settled[days]
            note = self.reflector.reflect_on_interim_outcome(
                final_decision=entry.get("decision", ""),
                raw_return=raw,
                alpha_return=alpha,
                elapsed_days=days,
                horizon_days=primary,
                benchmark_name=benchmark,
            )
            due.append({
                "ticker": ticker,
                "trade_date": entry["date"],
                "mandate": entry.get("mandate", ""),
                "horizon_days": days,
                "raw_return": raw,
                "alpha_return": alpha,
                "resolution_date": resolution_date,
                "note": note,
            })
        return due

    def resolve_instrument_context(self, ticker: str, asset_type: str = "stock",
                                   curr_date: str | None = None) -> str:
        """Resolve ticker identity once and return the full instrument context.

        Deterministic yfinance lookup (cached, fail-open) injected into a
        context string so every agent anchors to the real company instead of
        hallucinating one from the price chart (#814). Both the propagate()
        path and the CLI call this so the resolved identity reaches the whole
        graph regardless of entry point.
        """
        identity = resolve_instrument_identity(ticker)
        return build_instrument_context(ticker, asset_type, identity, curr_date)

    def _memory_as_of(self, trade_date) -> str | None:
        """Point-in-time cutoff for past-context lessons (#1251).

        A historical/backtest run (trade date before today) filters lessons to
        those already resolved by the trade date. A current-date run returns
        None, disabling the filter so live behavior and pre-migration entries
        (which have no stored resolution date) are unaffected.
        """
        td = str(trade_date)
        return td if td < datetime.now().strftime("%Y-%m-%d") else None

    def _run_signature(self, asset_type: str, portfolio=None) -> str:
        """Graph-shape inputs that must invalidate a checkpoint if changed.

        Keyed into the checkpoint thread ID so a resume under a different analyst
        selection, debate/risk depth, or asset mode starts fresh instead of
        silently continuing the previous graph (#1089).
        """
        parts = [
            "analysts=" + ",".join(self.selected_analysts),
            f"debate={self.config['max_debate_rounds']}",
            f"risk={self.config['max_risk_discuss_rounds']}",
            f"asset={asset_type}",
            f"mandate={self.mandate_name}",
            # None, an empty book and a changed book are three different runs.
            f"portfolio={portfolio.fingerprint() if portfolio is not None else 'none'}",
        ]
        # A mandate's own analysts are graph shape too: without this, a run
        # interrupted before a mandate gained analysts would resume into a graph
        # with nodes its checkpoint never saw. Appended only when present, so
        # every other signature -- and every checkpoint keyed on one -- is
        # unchanged.
        mandate_analysts = self.mandate.analysts if self.mandate is not None else ()
        if mandate_analysts:
            parts.append("mandate_analysts=" + ",".join(a.key for a in mandate_analysts))
        return "|".join(parts)

    def propagate(self, company_name, trade_date, asset_type: str = "stock", portfolio=None,
                  supersede: bool = False):
        """Run the trading agents graph for a company on a specific date.

        ``asset_type`` selects between the stock pipeline (default) and the
        crypto pipeline (``"crypto"``) shipped in #567 — the CLI auto-detects
        from the ticker; programmatic callers pass it explicitly. When
        ``checkpoint_enabled`` is set in config, the graph is recompiled with
        a per-ticker SqliteSaver so a crashed run can resume from the last
        successful node on a subsequent invocation with the same ticker+date.

        Returns ``(final_state, signal)`` where ``signal`` is one of the 5-tier
        ratings (Buy / Overweight / Hold / Underweight / Sell) or ``"REVIEW"``
        when the decision had no parseable rating (#1170); guard with
        ``tradingagents.agents.utils.rating.is_review`` before mapping it to the
        PortfolioRating enum.
        """
        trade_date = _validate_trade_date(trade_date)
        self.ticker = company_name

        with self.checkpoint_scope(company_name, trade_date, asset_type, portfolio) as thread_id_value:
            return self._run_graph(
                company_name, trade_date, asset_type=asset_type,
                checkpoint_thread_id=thread_id_value, portfolio=portfolio,
                supersede=supersede,
            )

    def begin_checkpoint(self, company_name, trade_date, asset_type: str = "stock", portfolio=None) -> str | None:
        """Recompile the graph with a per-ticker checkpointer and return the
        ``thread_id`` to inject into the stream/invoke ``config`` (or ``None``
        when checkpointing is disabled).

        Pair every call with :meth:`end_checkpoint` in a ``finally``. Both
        ``propagate`` (via :meth:`checkpoint_scope`) and the CLI stream path use
        this so ``--checkpoint`` actually resumes (#1249); previously the setup
        lived only inside ``propagate`` and the CLI streamed the checkpointer-less
        graph, making the flag a no-op.
        """
        self._resuming = False
        if not self.config.get("checkpoint_enabled"):
            return None
        signature = self._run_signature(asset_type, portfolio)
        self._checkpointer_ctx = get_checkpointer(self.config["data_cache_dir"], company_name)
        saver = self._checkpointer_ctx.__enter__()
        self.graph = self.workflow.compile(checkpointer=saver)

        step = checkpoint_step(
            self.config["data_cache_dir"], company_name, str(trade_date), signature
        )
        self._resuming = step is not None
        if step is not None:
            logger.info("Resuming from step %d for %s on %s", step, company_name, trade_date)
        else:
            logger.info("Starting fresh for %s on %s", company_name, trade_date)
        return thread_id(company_name, str(trade_date), signature)

    def checkpoint_input(self, init_state):
        """The value to stream/invoke: ``None`` to resume an existing checkpoint,
        else the initial state for a fresh run.

        LangGraph resumes an interrupted thread when invoked with ``None``;
        re-passing the initial state instead appends it through the message
        reducer, duplicating messages in the resumed state (#1249).
        """
        return None if self._resuming else init_state

    def end_checkpoint(self):
        """Restore the plain uncheckpointed graph after a checkpointed run."""
        if self._checkpointer_ctx is not None:
            self._checkpointer_ctx.__exit__(None, None, None)
            self._checkpointer_ctx = None
            self.graph = self.workflow.compile()
        self._resuming = False

    @contextmanager
    def checkpoint_scope(self, company_name, trade_date, asset_type: str = "stock", portfolio=None):
        """Context-manager form of begin/end_checkpoint for the propagate path."""
        try:
            yield self.begin_checkpoint(company_name, trade_date, asset_type, portfolio)
        finally:
            self.end_checkpoint()

    def clear_checkpoint_on_success(self, company_name, trade_date, asset_type: str = "stock", portfolio=None):
        """Drop a completed run's checkpoint so a later run starts fresh (#1249)."""
        if self.config.get("checkpoint_enabled"):
            clear_checkpoint(
                self.config["data_cache_dir"], company_name, str(trade_date),
                self._run_signature(asset_type, portfolio),
            )

    def save_reports(self, final_state, ticker, save_path=None) -> Path:
        """Write the markdown report tree for a completed run, like the CLI does.

        Programmatic callers get the same on-disk reports the CLI produces. Pass
        an explicit ``save_path`` or let it default under ``results_dir``.
        """
        if save_path is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path = (
                Path(self.config["results_dir"])
                / "reports"
                / f"{safe_ticker_component(ticker)}_{stamp}"
            )
        return write_report_tree(final_state, ticker, save_path)

    def create_run_state(self, company_name, trade_date, asset_type: str = "stock", portfolio=None):
        """Build a run's initial state; propagate() and the CLI both start here.

        Settles this ticker's pending decisions first, then injects the lessons
        known by the trade date for the Portfolio Manager (#1251) and the
        resolved instrument identity for every agent (#814). An entry point that
        assembled the state itself would skip the decision log.
        """
        self._resolve_pending_entries(company_name)
        return self.propagator.create_initial_state(
            company_name,
            trade_date,
            asset_type=asset_type,
            past_context=self.memory_log.get_past_context(
                company_name, as_of=self._memory_as_of(trade_date)
            ),
            instrument_context=self.resolve_instrument_context(company_name, asset_type, trade_date),
            portfolio_context=portfolio.render(company_name) if portfolio is not None else "",
            mandate=self.mandate_name,
            mandate_context=self.mandate_context,
        )

    def settle_pending(self, company_name):
        """Settle this ticker's decisions whose holding window has now traded.

        A run settles the ticker's earlier decisions on its way in, so the most
        recent one stays pending until the next run for that ticker. A caller
        that is done analyzing a ticker (a backtest sweep, a scheduled job) calls
        this to settle it now.
        """
        self._resolve_pending_entries(company_name)

    def record_decision(self, company_name, trade_date, final_state, supersede: bool = False):
        """Log a finished run's decision for reflection on the next same-ticker run."""
        decision = final_state.get("final_trade_decision")
        if not decision:
            logger.warning("No final decision for %s on %s; nothing logged", company_name, trade_date)
            return
        written = self.memory_log.store_decision(
            ticker=company_name, trade_date=trade_date, final_trade_decision=decision,
            mandate=self.mandate_name, supersede=supersede,
        )
        if not written:
            logger.info(
                "%s on %s under mandate %r is already logged; this run was not "
                "recorded. Pass supersede=True (CLI: --supersede) to retire the "
                "existing entry and record this one in its place.",
                company_name, trade_date, self.mandate_name or "none",
            )

    def _run_graph(self, company_name, trade_date, asset_type: str = "stock",
                   checkpoint_thread_id: str | None = None, portfolio=None,
                   supersede: bool = False):
        """Execute the graph and write the resulting state to disk and memory log."""
        init_agent_state = self.create_run_state(company_name, trade_date, asset_type, portfolio)
        args = self.propagator.get_graph_args()

        # Inject the checkpoint thread_id (from checkpoint_scope) so the same
        # ticker+date+graph-shape resumes; a different one starts fresh (#1089).
        if checkpoint_thread_id is not None:
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = checkpoint_thread_id

        # None resumes an existing checkpoint; init_agent_state starts fresh (#1249).
        graph_input = self.checkpoint_input(init_agent_state)
        if self.debug:
            trace = []
            last_printed = None
            for chunk in self.graph.stream(graph_input, **args):
                if chunk["messages"]:
                    msg = chunk["messages"][-1]
                    # Nodes after the trader don't append to messages, so the
                    # same trailing message repeats across chunks. Print it only
                    # when it changes (#1027); the trace/state merge is unchanged.
                    signature = (type(msg).__name__, getattr(msg, "content", None))
                    if signature != last_printed:
                        msg.pretty_print()
                        last_printed = signature
                    trace.append(chunk)
            # Streamed chunks are per-node deltas. Merge them so the returned
            # state matches what graph.invoke() yields in the non-debug path.
            final_state = {}
            for chunk in trace:
                final_state.update(chunk)
        else:
            final_state = self.graph.invoke(graph_input, **args)

        # Store current state for reflection.
        self.curr_state = final_state

        # Log state to disk.
        self._log_state(trade_date, final_state)

        self.record_decision(company_name, trade_date, final_state, supersede)

        # Clear checkpoint on successful completion to avoid stale state.
        self.clear_checkpoint_on_success(company_name, trade_date, asset_type, portfolio)

        return final_state, self.process_signal(final_state["final_trade_decision"])

    def _log_state(self, trade_date, final_state):
        """Log the final state to a JSON file."""
        self.log_states_dict[str(trade_date)] = {
            "company_of_interest": final_state["company_of_interest"],
            "trade_date": final_state["trade_date"],
            "market_report": final_state["market_report"],
            "sentiment_report": final_state["sentiment_report"],
            "news_report": final_state["news_report"],
            "fundamentals_report": final_state["fundamentals_report"],
            "investment_debate_state": {
                "bull_history": final_state["investment_debate_state"]["bull_history"],
                "bear_history": final_state["investment_debate_state"]["bear_history"],
                "history": final_state["investment_debate_state"]["history"],
                "current_response": final_state["investment_debate_state"][
                    "current_response"
                ],
                "judge_decision": final_state["investment_debate_state"][
                    "judge_decision"
                ],
            },
            "trader_investment_decision": final_state["trader_investment_plan"],
            "risk_debate_state": {
                "aggressive_history": final_state["risk_debate_state"]["aggressive_history"],
                "conservative_history": final_state["risk_debate_state"]["conservative_history"],
                "neutral_history": final_state["risk_debate_state"]["neutral_history"],
                "history": final_state["risk_debate_state"]["history"],
                "judge_decision": final_state["risk_debate_state"]["judge_decision"],
            },
            "investment_plan": final_state["investment_plan"],
            "final_trade_decision": final_state["final_trade_decision"],
        }

        # Save to file. Reject ticker values that would escape the
        # results directory when joined as a path component.
        safe_ticker = safe_ticker_component(self.ticker)
        directory = Path(self.config["results_dir"]) / safe_ticker / "TradingAgentsStrategy_logs"
        directory.mkdir(parents=True, exist_ok=True)

        log_path = directory / f"full_states_log_{trade_date}.json"
        with open(log_path, "w", encoding="utf-8") as f:
            # Reports can be in any language and this file is read by a person.
            json.dump(self.log_states_dict[str(trade_date)], f, indent=4, ensure_ascii=False)

    def process_signal(self, full_signal):
        """Process a signal to extract the core decision."""
        return self.signal_processor.process_signal(full_signal)
