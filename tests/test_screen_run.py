"""Tier 3: a screen's picks and control adjudicated as one resumable sweep."""

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import cli.main as m
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.screener import manifest as mf, run as sr

runner = CliRunner()


def _manifest(run_id="2022-03-01_equity_momentum_101500", as_of="2022-03-01",
              mandate="equity_momentum", picks=("AAA", "BBB"), controls=("CCC",), created="2026-09-18T10:15:00"):
    return mf.ScreenManifest(
        run_id=run_id, mandate=mandate, as_of=as_of, created=created, universe_size=10, tiers=[],
        ordering_signal="x", picks=[{"symbol": s, "rank": i + 1, "value": 0.1} for i, s in enumerate(picks)],
        controls=[{"symbol": s, "value": 0.0} for s in controls], control_seed=1, eligible_count=5,
    )


@pytest.fixture
def config(tmp_path):
    return {"results_dir": str(tmp_path), "memory_log_path": str(tmp_path / "live.md"),
            "data_vendors": {"core_stock_apis": "yfinance", "technical_indicators": "yfinance",
                             "fundamental_data": "yfinance"}}


def _decide(config, manifest, ticker, mandate=None, date=None):
    log = TradingMemoryLog({"memory_log_path": str(sr.sweep_log_path(manifest, config))})
    log.store_decision(ticker, date or manifest.as_of, "Rating: Buy",
                       mandate=manifest.mandate if mandate is None else mandate)


# --- the sweep ----------------------------------------------------------------------------


class TestSweep:

    def test_picks_and_controls_run_together_under_the_screens_mandate_and_date(self, config):
        calls = {}
        def runner_(tickers, dates, cfg, **kw):
            calls.update(tickers=tickers, dates=dates, **kw)
        sr.run(_manifest(), config, ["market"], runner=runner_)
        assert calls["tickers"] == ["AAA", "BBB", "CCC"]
        assert calls["dates"] == ["2022-03-01"]
        assert calls["mandate"] == "equity_momentum"
        assert calls["selected_analysts"] == ["market"]

    def test_the_sweep_id_is_derived_from_the_screen_so_it_resumes(self, config):
        calls = {}
        sr.run(_manifest(), config, runner=lambda t, d, c, **kw: calls.update(kw))
        assert calls["run_id"] == "scr_20220301_momentum_101500"

    @pytest.mark.parametrize("mandate", ["equity_value", "equity_momentum", "", "a_very_long_future_mandate_name"])
    def test_the_sweep_id_passes_the_real_run_id_validator(self, mandate):
        """The first live run was refused: 'screen_' + a screen id is 40 characters."""
        from tradingagents.dataflows.utils import safe_ticker_component

        man = _manifest(run_id=f"2022-03-01_{mandate or 'none'}_171242", mandate=mandate)
        assert safe_ticker_component(sr.sweep_id(man)) == sr.sweep_id(man)

    def test_an_unmandated_screen_runs_unmandated_not_under_the_environments(self, config):
        calls = {}
        sr.run(_manifest(mandate=""), config, runner=lambda t, d, c, **kw: calls.update(kw))
        assert calls["mandate"] == "", "None would defer to TRADINGAGENTS_MANDATE"

    def test_a_name_listed_twice_runs_once(self, config):
        calls = {}
        sr.run(_manifest(picks=("AAA",), controls=("AAA", "CCC")), config,
               runner=lambda t, d, c, **kw: calls.update(tickers=t))
        assert calls["tickers"] == ["AAA", "CCC"]


class TestRemaining:

    def test_decided_names_are_not_run_again(self, config):
        man = _manifest()
        _decide(config, man, "AAA")
        assert sr.remaining(man, config) == ["BBB", "CCC"]

    def test_a_decision_under_another_mandate_or_date_does_not_count(self, config):
        man = _manifest()
        _decide(config, man, "AAA", mandate="equity_value")
        _decide(config, man, "BBB", date="2022-03-02")
        assert sr.remaining(man, config) == ["AAA", "BBB", "CCC"]

    def test_unfinished_lists_only_screens_with_work_left(self, config):
        done, todo = _manifest(run_id="2022-03-01_equity_momentum_1"), _manifest(run_id="2022-03-01_equity_momentum_2")
        for man in (done, todo):
            mf.save_manifest(man, config)
        for t in ("AAA", "BBB", "CCC"):
            _decide(config, done, t)
        assert [p.manifest.run_id for p in sr.unfinished(config)] == ["2022-03-01_equity_momentum_2"]


class TestVendors:

    def test_a_historical_screen_adds_a_price_fallback_for_delisted_names(self, config, monkeypatch):
        monkeypatch.setattr(sr, "get_current_date", lambda: "2026-09-18")
        vendors = sr.config_for(_manifest(as_of="2022-03-01"), config)["data_vendors"]
        assert vendors["core_stock_apis"] == "yfinance,alpha_vantage"
        assert vendors["technical_indicators"] == "yfinance,alpha_vantage"
        assert vendors["fundamental_data"] == "yfinance", "only price categories change"

    def test_a_live_screen_runs_on_the_configured_vendors(self, config, monkeypatch):
        monkeypatch.setattr(sr, "get_current_date", lambda: "2026-09-18")
        assert sr.config_for(_manifest(as_of="2026-09-18"), config) is config

    def test_a_chain_already_including_it_is_left_alone(self, config, monkeypatch):
        monkeypatch.setattr(sr, "get_current_date", lambda: "2026-09-18")
        config["data_vendors"]["core_stock_apis"] = "alpha_vantage,yfinance"
        vendors = sr.config_for(_manifest(), config)["data_vendors"]
        assert vendors["core_stock_apis"] == "alpha_vantage,yfinance"


class TestFind:

    def test_by_short_id_full_id_and_latest(self, config):
        old = _manifest(run_id="2022-03-01_equity_momentum_101500", created="2026-09-17T10:00:00")
        new = _manifest(run_id="2022-09-01_equity_value_120000", created="2026-09-18T12:00:00")
        for man in (old, new):
            mf.save_manifest(man, config)
        assert sr.find(config, "101500").run_id == old.run_id
        assert sr.find(config, old.run_id).run_id == old.run_id
        assert sr.find(config, "latest").run_id == new.run_id

    def test_no_match_and_no_screens_say_so(self, config):
        with pytest.raises(ValueError, match="no screens have been saved"):
            sr.find(config, "latest")
        mf.save_manifest(_manifest(), config)
        with pytest.raises(ValueError, match="no screen matches"):
            sr.find(config, "999999")


# --- CLI -------------------------------------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch, config, tmp_path):
    runs = []

    def fake_run_backtest(tickers, dates, cfg, **kwargs):
        runs.append(dict(tickers=tickers, dates=dates, **kwargs))
        return SimpleNamespace(log_path=tmp_path / "log.md", cells_run=len(tickers), skipped=0,
                               failures=[], settlement_failures=[])

    monkeypatch.setattr(m, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(m, "run_backtest", fake_run_backtest)
    monkeypatch.setattr(m, "summarize", lambda log: SimpleNamespace(render=lambda: ""))
    return runs


def _plain(text):
    import re
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", text).split())


def test_screen_run_by_id(cli, config):
    mf.save_manifest(_manifest(), config)
    result = runner.invoke(m.app, ["screen-run", "101500", "--analysts", "market,fundamentals"])
    assert result.exit_code == 0, result.output
    assert cli[0]["tickers"] == ["AAA", "BBB", "CCC"]
    assert cli[0]["selected_analysts"] == ["market", "fundamentals"]
    assert "picks AAA, BBB; controls CCC" in _plain(result.output)


def test_screen_run_all_runs_only_unfinished_screens(cli, config):
    done, todo = _manifest(run_id="2022-03-01_equity_momentum_1"), _manifest(run_id="2022-03-01_equity_momentum_2")
    for man in (done, todo):
        mf.save_manifest(man, config)
    for t in ("AAA", "BBB", "CCC"):
        _decide(config, done, t)
    result = runner.invoke(m.app, ["screen-run", "--all"])
    assert result.exit_code == 0, result.output
    assert [r["run_id"] for r in cli] == ["scr_20220301_momentum_2"]


def test_dry_run_spends_nothing(cli, config):
    mf.save_manifest(_manifest(), config)
    result = runner.invoke(m.app, ["screen-run", "--all", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert cli == []
    assert "3 names (AAA, BBB, CCC), about 24 minutes" in _plain(result.output)


def test_nothing_left_says_so(cli, config):
    man = _manifest()
    mf.save_manifest(man, config)
    for t in ("AAA", "BBB", "CCC"):
        _decide(config, man, t)
    result = runner.invoke(m.app, ["screen-run", "latest"])
    assert result.exit_code == 0 and cli == []
    assert "already decided" in _plain(result.output)


def test_an_id_and_all_together_is_refused(cli):
    result = runner.invoke(m.app, ["screen-run", "101500", "--all"])
    assert result.exit_code == 1


def test_screen_with_run_adjudicates_what_it_just_found(cli, config, monkeypatch):
    import tradingagents.screener as screener
    import tradingagents.screener.review as review

    man = _manifest(as_of="2026-09-18", run_id="2026-09-18_equity_momentum_101500")
    monkeypatch.setattr(screener, "run_screen", lambda *a, **k: SimpleNamespace(manifest=man, excluded=[]))
    monkeypatch.setattr(screener, "save_manifest", lambda *a: "manifest.json")
    monkeypatch.setattr(review, "render_screen", lambda *a: "report")
    result = runner.invoke(m.app, ["screen", "--mandate", "equity_momentum", "--date", "2026-09-18", "--run"])
    assert result.exit_code == 0, result.output
    assert cli[0]["run_id"] == "scr_20260918_momentum_101500"
    assert cli[0]["dates"] == ["2026-09-18"]


# --- the state log: a sweep's only record of the mandate analysts' reports -------------


def test_the_state_log_keeps_the_mandate_analysts_reports(tmp_path):
    """Lost once in the v0.5.0 merge, found only by a live screen-run: the debate
    quoted the Momentum and Growth analysts, but the cell's JSON had no trace of them."""
    import json

    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = object.__new__(TradingAgentsGraph)
    graph.config = {"results_dir": str(tmp_path)}
    graph.ticker = "AAWW"
    graph.log_states_dict = {}
    empty_debate = {"bull_history": "", "bear_history": "", "history": "",
                    "current_response": "", "judge_decision": "", "count": 0}
    empty_risk = {"aggressive_history": "", "conservative_history": "", "neutral_history": "",
                  "history": "", "judge_decision": "", "latest_speaker": "",
                  "current_aggressive_response": "", "current_conservative_response": "",
                  "current_neutral_response": "", "count": 0}
    graph._log_state("2022-06-01", {
        "company_of_interest": "AAWW", "trade_date": "2022-06-01", "market_report": "m",
        "sentiment_report": "", "news_report": "", "fundamentals_report": "",
        "mandate_reports": {"momentum": "Trend intact above the rising 50-day.", "growth": "Revisions up."},
        "investment_plan": "", "trader_investment_plan": "", "final_trade_decision": "Rating: Sell",
        "investment_debate_state": empty_debate, "risk_debate_state": empty_risk,
    })
    written = json.loads(next(tmp_path.rglob("full_states_log*.json")).read_text(encoding="utf-8"))
    assert written["mandate_reports"] == {"momentum": "Trend intact above the rising 50-day.",
                                          "growth": "Revisions up."}


# --- running a screen under an overlay (LEAPS) ------------------------------------------


class TestOverlay:
    """equity_momentum_leaps decides direction exactly as equity_momentum, so a
    momentum screen may be run under it and still be scored as a momentum screen."""

    def test_serves(self):
        from tradingagents.mandates import serves

        assert serves("equity_momentum", "equity_momentum")
        assert serves("equity_momentum_leaps", "equity_momentum")
        assert not serves("equity_momentum", "equity_momentum_leaps")
        assert not serves("equity_value", "equity_momentum")
        assert not serves("equity_momentum_leaps", "equity_value")
        assert not serves("equity_momentum_leaps", "")

    def test_the_names_run_under_the_overlay(self, config):
        calls = {}
        sr.run(_manifest(), config, runner=lambda t, d, c, **kw: calls.update(kw),
               mandate="equity_momentum_leaps")
        assert calls["mandate"] == "equity_momentum_leaps"
        assert calls["run_id"] == "scr_20220301_momentum_101500"

    def test_only_an_overlay_of_the_screens_mandate_may_stand_in(self, config):
        with pytest.raises(ValueError, match="not an overlay"):
            sr.plan(_manifest(), config, "equity_value")
        with pytest.raises(ValueError, match="not an overlay"):
            sr.plan(_manifest(mandate="equity_value"), config, "equity_momentum_leaps")

    def test_resuming_counts_only_the_overlays_own_decisions(self, config):
        """Names decided under plain momentum are not yet decided under LEAPS."""
        m = _manifest()
        _decide(config, m, "AAA")                                   # plain momentum
        _decide(config, m, "BBB", mandate="equity_momentum_leaps")
        assert sr.plan(m, config).todo == ["BBB", "CCC"]
        assert sr.plan(m, config, "equity_momentum_leaps").todo == ["AAA", "CCC"]

    def test_all_selects_only_screens_the_overlay_can_serve(self, config, monkeypatch):
        momentum, value = _manifest(), _manifest(run_id="2022-03-01_equity_value_1", mandate="equity_value")
        monkeypatch.setattr(sr, "load_manifests", lambda cfg, mandate=None: [momentum, value])
        plans = sr.unfinished(config, run_as="equity_momentum_leaps")
        assert [p.manifest.run_id for p in plans] == [momentum.run_id]
        assert plans[0].mandate == "equity_momentum_leaps"
