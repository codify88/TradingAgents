"""Desk: what it shows, what it lets the operator do, and who else it refuses."""
from __future__ import annotations

import json
import math

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from tests.test_trading import FakeBroker, _plan  # noqa: E402
from tradingagents.desk import views  # noqa: E402
from tradingagents.desk.app import create_app  # noqa: E402
from tradingagents.lab import adopted, replay as rp, report as lr  # noqa: E402
from tradingagents.trading.broker import BrokerError  # noqa: E402

TOKEN = "test-token"


def _result(signal, tune_t, test_sel, test_t, net=0.001):
    s = rp.Stats(100, 0.01, tune_t, 0.6, 0.0, 1.0, 0.0, 0.0, 0.0, independent=100)
    t = rp.Stats(50, test_sel, test_t, 0.6, net, math.nan, 0.0, 0.0, 0.0, independent=50)
    return rp.Result(rp.Variant(signal, 8, 5e6), s, t)


@pytest.fixture
def config(tmp_path):
    return {"data_cache_dir": str(tmp_path / "cache"), "results_dir": str(tmp_path / "results"),
            "trading_dir": str(tmp_path / "trading")}


def _lab_run(config, key="standard", results=None):
    spec = rp.STRATEGIES[key]
    results = results or [_result("reversal_5d", 9.0, 0.004, 3.0), _result("momentum_21d", 2.0, 0.001, 0.5),
                          _result("random", 0.1, 0.0, 0.0)]
    v = lr.verdict(spec, results, trials=10)
    lr.save_results(config, key, spec, results, v, spec.split)
    return v


def _client(config, broker_factory=None, **kw):
    app = create_app(config, token=TOKEN, broker_factory=broker_factory,
                     allowed_hosts=kw.pop("allowed_hosts", ("testserver",)))
    return TestClient(app, **kw)


def _no_broker(config):
    raise BrokerError("ALPACA_API_KEY and ALPACA_SECRET_KEY are not set")


# --- the lab -------------------------------------------------------------------------


@pytest.mark.unit
def test_results_are_saved_as_data_ranked_by_tuning_t_with_nan_as_null(config):
    _lab_run(config)
    data = lr.load_results(config, "standard")
    assert [r["variant"] for r in data["variants"]] == ["reversal_5d/p8/dv5m", "momentum_21d/p8/dv5m",
                                                        "random/p8/dv5m"]
    assert data["variants"][0]["test"]["t_net"] is None  # NaN is not JSON
    assert data["variants"][2]["null"] is True
    assert data["verdict"]["passes"] is True and data["verdict"]["candidate"] == "reversal_5d/p8/dv5m"
    assert lr.load_results(config, "value") is None


@pytest.mark.unit
def test_an_adoption_records_its_evidence_and_whether_it_was_the_lab_pick(config):
    _lab_run(config)
    pick = adopted.adopt(config, "standard", "reversal_5d/p8/dv5m", "clears the bar")
    assert pick["evidence"]["lab_candidate"] is True
    assert pick["evidence"]["figures"]["tune"]["t"] == 9.0
    call = adopted.adopt(config, "standard", "momentum_21d/p8/dv5m", "my call")
    assert call["evidence"]["lab_candidate"] is False
    assert call["evidence"]["verdict"]["candidate"] == "reversal_5d/p8/dv5m"
    # No saved run: still adoptable, with no evidence to record.
    assert adopted.adopt(config, "momentum", "excess_12m/p8/dv5m/top60")["evidence"] is None


@pytest.mark.unit
def test_the_lab_view_says_what_is_live_and_which_screens_read_adoptions(config):
    _lab_run(config)
    lab = views.lab(config)["strategies"]
    assert all(lab[k]["reads_live"] for k in ("standard", "momentum", "value"))
    assert lab["standard"]["default"] == "liquidity/p8/dv5m" and lab["standard"]["current"] is None
    views.adopt(config, "standard", "reversal_5d/p8/dv5m", "  why  ")
    lab = views.lab(config)["strategies"]
    assert lab["standard"]["current"]["variant"] == "reversal_5d/p8/dv5m"
    assert lab["standard"]["current"]["reason"] == "why"
    assert lab["standard"]["history"][0]["variant"] == "reversal_5d/p8/dv5m"


@pytest.mark.unit
def test_an_adoption_needs_a_reason_and_a_real_variant(config):
    with pytest.raises(ValueError, match="say why"):
        views.adopt(config, "standard", "reversal_5d/p8/dv5m", " ")
    with pytest.raises(ValueError, match="not a standard variant"):
        views.adopt(config, "standard", "nonsense", "why")
    with pytest.raises(ValueError, match="null"):
        views.adopt(config, "standard", "random/p8/dv5m", "why")


# --- the server's guards -------------------------------------------------------------


@pytest.mark.unit
def test_the_page_carries_the_token_and_the_api_serves_the_lab(config):
    _lab_run(config)
    c = _client(config)
    assert TOKEN in c.get("/").text
    body = c.get("/api/lab").json()
    assert body["strategies"]["standard"]["results"]["verdict"]["passes"] is True
    assert c.get("/api/lab/standard/report").status_code == 404  # no markdown report written
    assert c.get("/static/desk.js").status_code == 200


@pytest.mark.unit
def test_actions_need_the_token_and_refuse_foreign_origins(config):
    _lab_run(config)
    c = _client(config)
    body = {"strategy": "standard", "variant": "reversal_5d/p8/dv5m", "reason": "why"}
    assert c.post("/api/lab/adopt", json=body).status_code == 403
    assert c.post("/api/lab/adopt", json=body, headers={"X-Desk-Token": "wrong"}).status_code == 403
    assert c.post("/api/lab/adopt", json=body, headers={"X-Desk-Token": TOKEN,
                                                         "Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/trade/halt", json={}).status_code == 403
    assert adopted.current(config, "standard") is None
    r = c.post("/api/lab/adopt", json=body, headers={"X-Desk-Token": TOKEN, "Origin": "http://testserver"})
    assert r.status_code == 200 and adopted.current(config, "standard")["variant"] == "reversal_5d/p8/dv5m"
    bad = c.post("/api/lab/adopt", json={**body, "reason": ""}, headers={"X-Desk-Token": TOKEN})
    assert bad.status_code == 400 and "say why" in bad.json()["error"]


@pytest.mark.unit
def test_only_loopback_host_names_are_answered(config):
    # The real app allows 127.0.0.1 and localhost only: another name (DNS rebinding) is refused.
    c = TestClient(create_app(config, token=TOKEN))
    assert c.get("/api/lab").status_code == 400
    ok = TestClient(create_app(config, token=TOKEN), base_url="http://127.0.0.1:8765")
    assert ok.get("/api/lab").status_code == 200


# --- trading -------------------------------------------------------------------------


@pytest.mark.unit
def test_trading_shows_the_account_and_sends_a_plan_once(config):
    broker = FakeBroker()
    p = _plan(config, broker, [("AAA", 1.0), ("BBB", 1.0)], day="2030-01-07")
    c = _client(config, broker_factory=lambda cfg: broker)
    t = c.get("/api/trading").json()
    assert t["broker"]["account"]["paper"] is True and t["broker"]["account"]["equity"] == 100_000.0
    assert t["pending"]["id"] == p.id and len(t["pending"]["orders"]) == 2
    h = {"X-Desk-Token": TOKEN}
    r = c.post("/api/trade/submit", json={"plan_id": p.id}, headers=h)
    assert r.status_code == 200 and len(broker.orders_) == 2
    again = c.post("/api/trade/submit", json={"plan_id": p.id}, headers=h).json()["result"]
    assert "submitted; nothing sent" in again and len(broker.orders_) == 2
    assert c.get("/api/trading").json()["pending"] is None


@pytest.mark.unit
def test_halt_works_without_the_broker_and_the_rest_explain_why_not(config):
    c = _client(config, broker_factory=_no_broker)
    h = {"X-Desk-Token": TOKEN}
    t = c.get("/api/trading").json()
    assert t["broker"]["available"] is False and "ALPACA_API_KEY" in t["broker"]["error"]
    assert c.post("/api/trade/halt", json={"reason": "test"}, headers=h).status_code == 200
    t = c.get("/api/trading").json()
    assert t["book"]["halted"] is True and "halted" in t["refusal"]
    r = c.post("/api/trade/reconcile", json={}, headers=h)
    assert r.status_code == 400 and "broker is unavailable" in r.json()["error"]
    assert c.post("/api/trade/resume", json={}, headers=h).status_code == 200
    assert c.get("/api/trading").json()["book"]["halted"] is False


@pytest.mark.unit
def test_a_plan_id_is_a_name_not_a_path(config):
    c = _client(config, broker_factory=lambda cfg: FakeBroker())
    h = {"X-Desk-Token": TOKEN}
    for bad in ("../../etc/passwd", "", "a/b"):
        r = c.post("/api/trade/submit", json={"plan_id": bad}, headers=h)
        assert r.status_code == 400, bad
    assert c.post("/api/trade/liquidate", json={}, headers=h).status_code == 404


@pytest.mark.unit
def test_the_cli_says_the_next_screen_uses_an_adoption(config, monkeypatch):
    from typer.testing import CliRunner

    import cli.main as cm

    monkeypatch.setattr(cm, "DEFAULT_CONFIG", config)
    runner = CliRunner()

    def run(*args):  # Rich wraps at the terminal width
        return " ".join(runner.invoke(cm.app, ["lab", "adopt", *args]).output.split())

    assert "the next screen uses it" in run("momentum", "excess_12m/p8/dv5m/top60")
    assert "the next screen uses it" in run("standard", "reversal_5d/p8/dv5m")
    assert "adopt a /topN variant" in run("momentum", "excess_12m/p8/dv5m")
    rows = [json.loads(x) for x in (adopted.lab_dir(config) / "adopted.jsonl").read_text().splitlines()]
    assert [r["strategy"] for r in rows] == ["momentum", "standard"]
