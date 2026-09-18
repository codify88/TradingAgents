"""P4 screener: universe, tiers, exclusion-only narrowing, controls, review.

The two properties that matter most are structural rather than numerical:
narrowing happens by exclusion and not by the thesis, and every run carries a
random control drawn from the eligible names the ranking rejected. Both are
asserted directly here, because both are easy to erode by accident later.
"""

import math
from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.mandates import get_mandate
from tradingagents.mandates.tools.quality import Screen
from tradingagents.screener import manifest as mf, prices, screen, universe
from tradingagents.screener.review import (
    GroupOutcome,
    render_performance,
    render_screen,
    score_manifest,
)

LISTING_CSV = """symbol,name,exchange,assetType,ipoDate,delistingDate,status
AAA,Alpha Inc,NYSE,Stock,2010-01-04,null,Active
BBB,Beta Corp,NASDAQ,Stock,2012-05-01,null,Active
CCC,Gamma Ltd,NYSE,Stock,2026-09-01,null,Active
DDD,Delta SA,OTC,Stock,2009-01-01,null,Active
EEE,Epsilon ETF,NYSE,ETF,2009-01-01,null,Active
FFF,Zeta Inc,NYSE,Stock,2008-01-01,2025-01-01,Delisted
GGG,Eta Inc,NYSE,Stock,2008-01-01,2026-01-01,Active
HHH,Theta Inc,NYSE,Stock,,null,Active
"""


def _frame(n=300, price=50.0, volume=1e6, rising=True, step=0.05):
    idx = pd.bdate_range("2025-01-01", periods=n)
    step = step if rising else -step
    close = pd.Series([price + i * step for i in range(n)], index=idx)
    return pd.DataFrame({
        "Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close,
        "Volume": pd.Series([volume] * n, index=idx),
    })


# --- tier 0: universe ------------------------------------------------------


class TestUniverse:
    @pytest.fixture
    def listing(self):
        with patch.object(universe, "_make_api_request", return_value=LISTING_CSV):
            yield

    def test_keeps_only_listed_common_stock_on_a_real_exchange(self, listing):
        symbols = [c.symbol for c in universe.load_universe("2026-09-17")]
        assert "AAA" in symbols and "BBB" in symbols
        assert "EEE" not in symbols, "ETFs are not the universe"
        assert "DDD" not in symbols, "OTC is excluded categorically, not by liquidity"

    def test_a_recent_listing_has_no_history_to_screen_on(self, listing):
        assert "CCC" not in [c.symbol for c in universe.load_universe("2026-09-17")]

    def test_a_name_delisted_before_the_date_was_never_investable_on_it(self, listing):
        symbols = [c.symbol for c in universe.load_universe("2026-09-17")]
        assert "FFF" not in symbols
        assert "GGG" not in symbols, "delisted in January; not investable in September"

    def test_an_unparseable_listing_date_is_dropped_rather_than_assumed(self, listing):
        assert "HHH" not in [c.symbol for c in universe.load_universe("2026-09-17")]

    def test_the_cap_is_deterministic_so_a_capped_run_reproduces(self, listing):
        first = [c.symbol for c in universe.load_universe("2026-09-17", limit=1)]
        second = [c.symbol for c in universe.load_universe("2026-09-17", limit=1)]
        assert first == second == ["AAA"]


# --- tier 1: investability -------------------------------------------------


class TestInvestability:
    def test_a_liquid_priced_name_with_history_is_not_excluded(self):
        assert screen.investability_exclusions(_frame()) == []

    def test_an_illiquid_name_is_excluded(self):
        assert any("dollar volume" in r for r in
                   screen.investability_exclusions(_frame(volume=100)))

    def test_a_sub_five_dollar_name_is_excluded(self):
        assert any("price below" in r for r in
                   screen.investability_exclusions(_frame(price=2.0, step=0.0)))

    def test_too_little_history_to_judge_is_excluded(self):
        assert any("insufficient" in r for r in
                   screen.investability_exclusions(_frame(n=60)))

    def test_median_not_mean_dollar_volume(self):
        """One index-rebalance day must not make an untradeable name look liquid."""
        frame = _frame(volume=1_000)
        frame.loc[frame.index[-1], "Volume"] = 1e12
        assert prices.dollar_volume(frame) < screen.MIN_DOLLAR_VOLUME


# --- the core property: exclusion, not thesis ------------------------------


class TestExclusionsOnly:
    def test_only_tripped_excludes_not_watch_or_no_data(self):
        """A WATCH is the contestable call the debate exists to make, and NO DATA
        is not evidence. Excluding on either narrows the universe on the
        screener's judgement rather than on the mandate's rules."""
        mandate = get_mandate("equity_momentum")
        verdicts = [
            Screen("tripped rule", "TRIPPED", "e"),
            Screen("watch rule", "WATCH", "e"),
            Screen("no data rule", "NO DATA", "e"),
            Screen("clear rule", "CLEAR", "e"),
        ]
        with patch.object(screen.mo, "momentum_screens", return_value=verdicts):
            out = screen.price_exclusions(mandate, _frame(), _frame())
        assert out == ["tripped rule"]

    def test_the_value_mandate_has_no_price_thesis_exclusions(self):
        """Its disqualifiers are about the business, so price must not narrow it
        beyond investability -- otherwise a cheap stock in a downtrend, which is
        the mandate's whole point, never reaches the analysts."""
        assert screen.price_exclusions(get_mandate("equity_value"), _frame(rising=False), _frame()) == []

    def test_an_unmandated_screen_applies_no_thesis_exclusions(self):
        assert screen.price_exclusions(None, _frame(rising=False), _frame()) == []

    def test_an_unverifiable_name_is_excluded_with_a_stated_reason(self):
        """Unverifiable is not the same as eligible: it must not pass through
        unexamined into the expensive tier."""
        with patch.object(screen, "load_financials", side_effect=RuntimeError("vendor down")):
            rules, value = screen.fundamental_exclusions(
                get_mandate("equity_value"), "X", "2026-09-17", _frame())
        assert rules and "unavailable" in rules[0]
        assert math.isnan(value)


# --- the run: budget, ordering, controls -----------------------------------


def _run(monkeypatch, universe_symbols, frames, tripped=(), ordering=None, **kwargs):
    monkeypatch.setattr(
        screen, "load_universe",
        lambda as_of, limit=None: [universe.Candidate(s, s, "NYSE", "2010-01-01")
                                   for s in universe_symbols])
    monkeypatch.setattr(
        screen.prices, "download",
        lambda syms, start, end, **kw: prices.PriceData(
            frames={s: frames[s] for s in syms if s in frames}))
    monkeypatch.setattr(
        screen, "fundamental_exclusions",
        lambda m, sym, as_of, frame, limiter=None: (
            list(tripped.get(sym, [])) if tripped else [], float("nan")))
    if ordering is not None:
        monkeypatch.setattr(screen, "_momentum_ordering",
                            lambda frame, bench: ordering.get(id(frame), 0.0))
    kwargs.setdefault("requests_per_minute", 100_000)  # no pacing in tests
    return screen.run_screen("equity_momentum", "2026-09-17", {"results_dir": "/tmp"}, **kwargs)


class TestRun:
    @pytest.fixture
    def many(self):
        symbols = [f"S{i:02d}" for i in range(20)]
        frames = {s: _frame() for s in symbols}
        frames["SPY"] = _frame()
        return symbols, frames

    def test_the_control_is_disjoint_from_the_picks(self, monkeypatch, many):
        symbols, frames = many
        result = _run(monkeypatch, symbols, frames, picks=4, controls=3, control_seed=1)
        picks = set(result.manifest.pick_symbols)
        controls = set(result.manifest.control_symbols)
        assert len(picks) == 4 and len(controls) == 3
        assert not (picks & controls), "the comparison is ranked vs not-ranked"

    def test_the_control_is_drawn_from_eligible_names_only(self, monkeypatch, many):
        symbols, frames = many
        tripped = {s: ["ineligible"] for s in symbols[10:]}
        result = _run(monkeypatch, symbols, frames, tripped=tripped,
                      picks=3, controls=3, control_seed=1)
        assert set(result.manifest.control_symbols) <= set(result.eligible)
        assert not (set(result.manifest.control_symbols) & set(tripped))

    def test_the_seed_makes_the_control_reproducible(self, monkeypatch, many):
        symbols, frames = many
        a = _run(monkeypatch, symbols, frames, picks=3, controls=3, control_seed=99)
        b = _run(monkeypatch, symbols, frames, picks=3, controls=3, control_seed=99)
        assert a.manifest.control_symbols == b.manifest.control_symbols

    def test_a_pool_too_small_for_a_control_says_so_rather_than_silently_skipping(
        self, monkeypatch, many
    ):
        symbols, frames = many
        tripped = {s: ["ineligible"] for s in symbols[3:]}
        result = _run(monkeypatch, symbols, frames, tripped=tripped,
                      picks=5, controls=3, control_seed=1)
        assert result.manifest.controls == []
        assert any("No control group" in n for n in result.manifest.notes)

    def test_the_budget_cut_is_reported_as_its_own_tier(self, monkeypatch, many):
        """It shrinks the funnel between tiers without judging any name, so a
        silent cut would look like an exclusion that was never explained."""
        symbols, frames = many
        result = _run(monkeypatch, symbols, frames, fundamental_budget=5, picks=2, controls=1)
        tiers = {t["name"]: t for t in result.manifest.tiers}
        assert tiers["liquidity budget"]["kept"] == 5
        assert "not a judgement" in next(iter(tiers["liquidity budget"]["reasons"]))

    def test_the_ordering_signal_is_named_in_the_manifest(self, monkeypatch, many):
        """Whatever bias the ordering introduces must be visible, not buried in
        a composite score."""
        symbols, frames = many
        result = _run(monkeypatch, symbols, frames, picks=2, controls=1)
        assert result.manifest.ordering_signal == screen.MOMENTUM_ORDERING

    def test_every_dropped_name_carries_its_reason(self, monkeypatch, many):
        symbols, frames = many
        tripped = {s: ["ineligible"] for s in symbols[10:]}
        result = _run(monkeypatch, symbols, frames, tripped=tripped, picks=2, controls=1)
        for symbol, reasons in result.excluded.items():
            assert reasons, symbol


# --- manifest round trip ---------------------------------------------------


class TestManifest:
    def test_round_trip(self, tmp_path):
        config = {"results_dir": str(tmp_path)}
        manifest = mf.ScreenManifest(
            run_id="r1", mandate="equity_value", as_of="2026-09-17", created="now",
            universe_size=10, tiers=[], ordering_signal="sig",
            picks=[{"symbol": "AAA", "rank": 1, "value": 0.5}],
            controls=[{"symbol": "BBB", "value": 0.1}],
            control_seed=1, eligible_count=5,
        )
        mf.save_manifest(manifest, config)
        loaded = mf.load_manifests(config)
        assert len(loaded) == 1
        assert loaded[0].pick_symbols == ["AAA"]
        assert loaded[0].control_symbols == ["BBB"]

    def test_a_corrupt_manifest_is_skipped_not_fatal(self, tmp_path):
        config = {"results_dir": str(tmp_path)}
        (tmp_path / "screens").mkdir()
        (tmp_path / "screens" / "bad.json").write_text("{not json", encoding="utf-8")
        assert mf.load_manifests(config) == []

    def test_filtering_by_mandate(self, tmp_path):
        config = {"results_dir": str(tmp_path)}
        for i, name in enumerate(("equity_value", "equity_momentum")):
            mf.save_manifest(mf.ScreenManifest(
                run_id=f"r{i}", mandate=name, as_of="2026-09-17", created="now",
                universe_size=1, tiers=[], ordering_signal="s", picks=[], controls=[],
                control_seed=0, eligible_count=0), config)
        assert [m.mandate for m in mf.load_manifests(config, "equity_value")] == ["equity_value"]


# --- review ----------------------------------------------------------------


def _manifest(picks, controls):
    return mf.ScreenManifest(
        run_id="r1", mandate="equity_momentum", as_of="2026-09-17", created="now",
        universe_size=100,
        tiers=[{"name": "price", "examined": 100, "kept": 20, "dropped": 80,
                "reasons": {"illiquid": 80}}],
        ordering_signal=screen.MOMENTUM_ORDERING,
        picks=[{"symbol": s, "rank": i + 1, "value": 0.1} for i, s in enumerate(picks)],
        controls=[{"symbol": s, "value": 0.0} for s in controls],
        control_seed=1, eligible_count=len(picks) + len(controls),
    )


class _Log:
    def __init__(self, entries):
        self._entries = entries

    def load_entries(self):
        return self._entries


def _entry(ticker, alpha=None, pending=False, superseded=None):
    return {"ticker": ticker, "date": "2026-09-17", "alpha": alpha,
            "pending": pending, "superseded": superseded, "rating": "Buy",
            "mandate": "equity_momentum", "decision": "", "reflection": ""}


class TestPerformanceReview:
    def test_picks_and_controls_are_scored_separately(self):
        log = _Log([_entry("AAA", "+5.0%"), _entry("BBB", "-1.0%")])
        picks, control = score_manifest(_manifest(["AAA"], ["BBB"]), log)
        assert picks.mean_alpha == pytest.approx(0.05)
        assert control.mean_alpha == pytest.approx(-0.01)

    def test_an_unsettled_name_counts_as_pending_not_as_zero(self):
        """Treating an unsettled decision as a zero would drag both arms toward
        each other and make a real edge look like none."""
        log = _Log([_entry("AAA", pending=True)])
        picks, _ = score_manifest(_manifest(["AAA"], []), log)
        assert picks.settled == 0 and picks.pending == 1
        assert math.isnan(picks.mean_alpha)

    def test_a_superseded_decision_is_not_scored(self):
        log = _Log([_entry("AAA", "+9.9%", superseded="2026-09-18")])
        picks, _ = score_manifest(_manifest(["AAA"], []), log)
        assert picks.settled == 0

    def test_a_name_never_run_is_pending_not_missing(self):
        picks, _ = score_manifest(_manifest(["ZZZ"], []), _Log([]))
        assert picks.pending == 1

    def test_the_report_refuses_a_verdict_until_both_arms_settle(self, tmp_path):
        config = {"results_dir": str(tmp_path), "memory_log_path": str(tmp_path / "log.md")}
        mf.save_manifest(_manifest(["AAA"], ["BBB"]), config)
        out = render_performance(config)
        assert "Nothing has settled yet" in out

    def test_no_screens_says_so(self, tmp_path):
        assert "No screens have been run yet." in render_performance(
            {"results_dir": str(tmp_path), "memory_log_path": str(tmp_path / "log.md")})

    def test_the_screen_report_states_the_ordering_and_the_control_rationale(self):
        out = render_screen(_manifest(["AAA", "CCC"], ["BBB"]))
        assert screen.MOMENTUM_ORDERING in out
        assert "at random" in out
        assert "TRIPPED" in out, "the report must explain what excludes and what does not"
        assert "| 1 | AAA |" in out

    def test_group_outcome_knows_when_it_cannot_be_measured(self):
        assert not GroupOutcome("x", 0, 3, float("nan"), float("nan"), []).measurable


# --- pacing ----------------------------------------------------------------


class TestRateLimiter:
    def test_it_does_not_sleep_below_the_ceiling(self):
        import time

        from tradingagents.screener.throttle import RateLimiter

        limiter = RateLimiter(rate=100)
        t0 = time.monotonic()
        limiter.acquire(50)
        assert time.monotonic() - t0 < 0.5

    def test_it_blocks_once_the_window_is_full(self):
        import time

        from tradingagents.screener.throttle import RateLimiter

        limiter = RateLimiter(rate=2, window=0.3)
        t0 = time.monotonic()
        limiter.acquire(4)
        assert time.monotonic() - t0 >= 0.3

    def test_only_rate_limits_are_retried(self):
        """A missing filing is a fact about the company, not a transient state;
        retrying it spends the budget the throttle exists to protect."""
        from tradingagents.dataflows.errors import VendorRateLimitError
        from tradingagents.screener.throttle import with_retry

        calls = []

        def rate_limited():
            calls.append(1)
            if len(calls) < 2:
                raise VendorRateLimitError("slow down")
            return "ok"

        assert with_retry(rate_limited, base_delay=0.01) == "ok"
        assert len(calls) == 2

        def missing():
            calls.append(1)
            raise ValueError("no filings")

        with pytest.raises(ValueError):
            with_retry(missing, base_delay=0.01)
        assert len(calls) == 3, "a non-rate-limit failure is not retried"


def test_derivative_share_classes_never_reach_the_price_tier():
    """Warrants, units, rights and preferred series are filed under assetType
    'Stock'. They are not what any mandate is written about, and they cost a
    download apiece only to be dropped as 'no price history'."""
    csv_text = (
        "symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"
        "AAC-WS,Ares Acquisition - Warrants,NYSE,Stock,2010-01-01,null,Active\n"
        "ABR-P-D,Arbor Realty Pref D,NYSE,Stock,2010-01-01,null,Active\n"
        "AAPL,Apple Inc,NASDAQ,Stock,1980-12-12,null,Active\n"
    )
    with patch.object(universe, "_make_api_request", return_value=csv_text):
        assert [c.symbol for c in universe.load_universe("2026-09-17")] == ["AAPL"]


class TestVendorFailureIsNotAFinding:
    """A throttled vendor and a dead company look identical in a dict of frames.
    Conflating them lets an outage masquerade as thousands of companies with no
    data, while the screen emits a confident shortlist from what leaked through.
    """

    def test_an_empty_batch_is_a_vendor_failure_not_a_universe_of_dead_names(self):
        with patch("yfinance.download", return_value=pd.DataFrame()):
            data = prices.download(["AAA", "BBB"], "2025-01-01", "2026-01-01", attempts=1)
        assert data.frames == {}
        assert data.unavailable == {"AAA", "BBB"}
        assert data.failure_rate == 1.0

    def test_a_partial_batch_is_believed(self):
        """Some symbols answering and others not is a real fact about those
        others, not an outage."""
        frame = _frame(n=30)
        combined = pd.concat({"AAA": frame}, axis=1)
        with patch("yfinance.download", return_value=combined):
            data = prices.download(["AAA", "BBB"], "2025-01-01", "2026-01-01", attempts=1)
        assert set(data.frames) == {"AAA"}
        assert data.unavailable == set()

    def test_a_raising_batch_is_retried_then_marked_unavailable(self):
        with patch("yfinance.download", side_effect=RuntimeError("throttled")) as dl, \
             patch("time.sleep"):
            data = prices.download(["AAA"], "2025-01-01", "2026-01-01", attempts=3)
        assert dl.call_count == 3
        assert data.unavailable == {"AAA"}

    def test_a_widespread_outage_withholds_the_shortlist(self, monkeypatch):
        symbols = [f"S{i:02d}" for i in range(20)]
        monkeypatch.setattr(
            screen, "load_universe",
            lambda as_of, limit=None: [universe.Candidate(s, s, "NYSE", "2010-01-01")
                                       for s in symbols])
        # Only two of twenty answer; the rest are a vendor failure.
        good = {s: _frame() for s in symbols[:2]}
        monkeypatch.setattr(
            screen.prices, "download",
            lambda syms, start, end, **kw: prices.PriceData(
                frames={s: good[s] for s in syms if s in good},
                unavailable={s for s in syms if s not in good},
            ))
        result = screen.run_screen(
            "equity_momentum", "2026-09-17", {"results_dir": "/tmp"},
            picks=5, controls=2, requests_per_minute=100_000)

        assert not result.usable
        assert result.manifest.picks == []
        assert result.manifest.controls == []
        assert any("NO SHORTLIST" in n for n in result.manifest.notes)
        # The funnel is still reported, so the failure is visible.
        assert result.manifest.tiers

    def test_an_outage_does_not_spend_the_fundamentals_budget(self, monkeypatch):
        symbols = [f"S{i:02d}" for i in range(20)]
        monkeypatch.setattr(
            screen, "load_universe",
            lambda as_of, limit=None: [universe.Candidate(s, s, "NYSE", "2010-01-01")
                                       for s in symbols])
        monkeypatch.setattr(
            screen.prices, "download",
            lambda syms, start, end, **kw: prices.PriceData(
                frames={s: _frame() for s in syms[:2]},
                unavailable=set(syms[2:])))
        calls = []
        monkeypatch.setattr(
            screen, "fundamental_exclusions",
            lambda *a, **k: calls.append(1) or ([], float("nan")))
        screen.run_screen("equity_momentum", "2026-09-17", {"results_dir": "/tmp"},
                          picks=2, controls=1, requests_per_minute=100_000)
        assert calls == [], "no API budget is spent on a sample of the vendor's mood"


class TestReviewReadsWhereOutcomesLand:
    """screen -> backtest -> screen-review has to close as a loop."""

    def test_a_decision_under_another_mandate_is_not_scored(self):
        other = {**_entry("AAA", "+9.9%"), "mandate": "equity_value"}
        unmandated = {**_entry("AAA", "+9.9%"), "mandate": ""}
        picks, _ = score_manifest(_manifest(["AAA"], []), _Log([other, unmandated]))
        assert picks.settled == 0 and picks.pending == 1

    def test_entries_are_pooled_across_logs(self):
        picks, control = score_manifest(
            _manifest(["AAA"], ["BBB"]), _Log([_entry("AAA", "+5.0%")]), _Log([_entry("BBB", "-1.0%")]))
        assert (picks.settled, control.settled) == (1, 1)

    def test_the_latest_sweep_wins_when_a_name_ran_twice(self):
        picks, _ = score_manifest(
            _manifest(["AAA"], []), _Log([_entry("AAA", "+1.0%")]), _Log([_entry("AAA", "+3.0%")]))
        assert picks.mean_alpha == pytest.approx(0.03)

    def test_a_backtest_sweeps_outcomes_reach_the_review(self, tmp_path):
        """Running exactly the command the screen prints must feed the report."""
        from tradingagents.agents.utils.memory import TradingMemoryLog
        from tradingagents.screener.review import decision_logs

        config = {"results_dir": str(tmp_path), "memory_log_path": str(tmp_path / "live.md")}
        mf.save_manifest(_manifest(["AAA"], ["BBB"]), config)
        sweep = TradingMemoryLog({"memory_log_path": str(tmp_path / "backtest" / "20260918_1" / "trading_memory.md")})
        for ticker, alpha in (("AAA", 0.05), ("BBB", -0.01)):
            sweep.store_decision(ticker, "2026-09-17", "Rating: Buy", mandate="equity_momentum")
            sweep.update_with_outcome(ticker, "2026-09-17", alpha, alpha, 126, "noted",
                                      resolution_date="2027-03-19", mandate="equity_momentum")

        assert len(decision_logs(config)) == 2
        out = render_performance(config)
        assert "| r1 | momentum | 2026-09-17 | 1/1 | +5.0% | 1/1 | -1.0% | +6.0% |" in out
