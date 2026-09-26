"""Desk's app API: Today, passkey enrolment, and money actions that need a passkey."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from starlette.testclient import TestClient  # noqa: E402

from tests.desk_authenticator import SoftAuthenticator  # noqa: E402
from tests.test_trading import FakeBroker, _plan  # noqa: E402
from tradingagents.desk.app import create_app  # noqa: E402
from tradingagents.desk.passkeys import Passkeys  # noqa: E402

ORIGIN = "http://localhost:8810"


@pytest.fixture
def config(tmp_path):
    return {"data_cache_dir": str(tmp_path / "cache"), "results_dir": str(tmp_path / "results"),
            "trading_dir": str(tmp_path / "trading")}


class Desk:
    """The app as a browser at ``origin`` uses it: token from the session, Origin on every POST."""

    def __init__(self, config, broker=None, origin=ORIGIN, hosts=None, clock=None):
        self.broker = broker or FakeBroker()
        self.keys = Passkeys(config, clock=clock) if clock else Passkeys(config)
        app = create_app(config, broker_factory=lambda cfg: self.broker, passkeys=self.keys,
                         allowed_hosts=hosts)
        self.origin = origin
        self.c = TestClient(app, base_url=origin, client=("127.0.0.1", 50000))
        self.token = self.c.get("/api/v1/session").json()["token"]

    def post(self, path, body=None, token=True, origin=True):
        h = {}
        if token:
            h["X-Desk-Token"] = self.token
        if origin:
            h["Origin"] = self.origin
        return self.c.post(path, json=body or {}, headers=h)

    def enrol(self, device=None):
        device = device or SoftAuthenticator()
        code = self.keys.new_code()
        opts = self.post("/api/v1/passkeys/register/options", {"code": code})
        assert opts.status_code == 200, opts.text
        r = self.post("/api/v1/passkeys/register/verify",
                      {"credential": device.register(opts.json(), self.origin), "label": "phone"})
        assert r.status_code == 200, r.text
        return device

    def money(self, device, action, **body):
        subject = body.get("plan_id", "") if action == "submit" else ""
        opts = self.post("/api/v1/passkeys/options", {"action": action, "subject": subject})
        assert opts.status_code == 200, opts.text
        return self.post(f"/api/v1/trade/{action}", {**body, "assertion": device.assert_(opts.json(), self.origin)})


@pytest.mark.unit
def test_session_gives_the_token_and_says_whether_a_passkey_is_enrolled(config):
    d = Desk(config)
    s = d.c.get("/api/v1/session").json()
    assert s["token"] == d.token and s["host"] == "localhost" and s["passkeys"] == 0
    d.enrol()
    assert d.c.get("/api/v1/session").json()["passkeys"] == 1


@pytest.mark.unit
def test_enrolment_needs_the_one_time_code(config):
    d = Desk(config)
    assert d.post("/api/v1/passkeys/register/options", {"code": "WRONG"}).status_code == 400
    code = d.keys.new_code()
    assert d.post("/api/v1/passkeys/register/options", {"code": f"{code[:4]}-{code[4:].lower()}"}).status_code == 200
    again = d.post("/api/v1/passkeys/register/options", {"code": code})
    assert again.status_code == 400 and "expired" in again.json()["error"]  # one use


@pytest.mark.unit
def test_a_plan_is_approved_with_a_passkey_and_only_with_one(config):
    d = Desk(config)
    p = _plan(config, d.broker, [("AAA", 1.0), ("BBB", 1.0)], day="2030-01-07")
    phone = d.enrol()
    # Token alone is not enough.
    r = d.post("/api/v1/trade/submit", {"plan_id": p.id})
    assert r.status_code == 401 and not d.broker.orders_
    # An assertion made for another plan, or another action, is refused.
    opts = d.post("/api/v1/passkeys/options", {"action": "submit", "subject": "other-plan"}).json()
    r = d.post("/api/v1/trade/submit", {"plan_id": p.id, "assertion": phone.assert_(opts, d.origin)})
    assert r.status_code == 401 and "something else" in r.json()["error"]
    # The real thing.
    r = d.money(phone, "submit", plan_id=p.id)
    assert r.status_code == 200 and "sent 2 of 2" in r.json()["result"] and len(d.broker.orders_) == 2
    assert d.c.get("/api/v1/today").json()["plan"] is None


@pytest.mark.unit
def test_an_assertion_is_used_once_and_expires(config):
    now = [1_000.0]
    d = Desk(config, clock=lambda: now[0])
    phone = d.enrol()
    d.post("/api/v1/trade/halt", {"reason": "test"})
    opts = d.post("/api/v1/passkeys/options", {"action": "resume"}).json()
    assertion = phone.assert_(opts, d.origin)
    assert d.post("/api/v1/trade/resume", {"assertion": assertion}).status_code == 200
    replay = d.post("/api/v1/trade/resume", {"assertion": assertion})
    assert replay.status_code == 401 and "expired" in replay.json()["error"]
    opts = d.post("/api/v1/passkeys/options", {"action": "ack"}).json()
    now[0] += 121
    late = d.post("/api/v1/trade/ack", {"assertion": phone.assert_(opts, d.origin)})
    assert late.status_code == 401


@pytest.mark.unit
def test_a_device_that_did_not_verify_the_user_is_refused(config):
    d = Desk(config)
    phone = d.enrol()
    lazy = SoftAuthenticator(user_verified=False)
    lazy.key, lazy.credential_id = phone.key, phone.credential_id
    opts = d.post("/api/v1/passkeys/options", {"action": "resume"}).json()
    r = d.post("/api/v1/trade/resume", {"assertion": lazy.assert_(opts, d.origin)})
    assert r.status_code == 401 and "verified" in r.json()["error"]


@pytest.mark.unit
def test_an_unenrolled_device_and_a_foreign_origin_are_refused(config):
    d = Desk(config)
    d.enrol()
    stranger = SoftAuthenticator()
    opts = d.post("/api/v1/passkeys/options", {"action": "resume"}).json()
    r = d.post("/api/v1/trade/resume", {"assertion": stranger.assert_(opts, d.origin)})
    assert r.status_code == 401 and "not enrolled" in r.json()["error"]
    evil = d.c.post("/api/v1/passkeys/options", json={"action": "resume"},
                    headers={"X-Desk-Token": d.token, "Origin": "https://evil.example"})
    assert evil.status_code == 403


@pytest.mark.unit
def test_a_passkey_belongs_to_the_host_it_was_made_on(config):
    hosts = ("localhost", "127.0.0.1", "mac.tail.ts.net")
    local = Desk(config, hosts=hosts)
    phone = local.enrol()
    remote = Desk(config, broker=local.broker, origin="https://mac.tail.ts.net", hosts=hosts)
    r = remote.post("/api/v1/passkeys/options", {"action": "resume"})
    assert r.status_code == 400 and "no passkey is enrolled" in r.json()["error"]
    remote.enrol()
    assert remote.money(remote.enrol(), "resume").status_code == 200
    assert local.money(phone, "resume").status_code == 200


@pytest.mark.unit
def test_halt_needs_no_passkey_and_halting_blocks_approval(config):
    d = Desk(config)
    p = _plan(config, d.broker, [("AAA", 1.0)], day="2030-01-07")
    phone = d.enrol()
    assert d.post("/api/v1/trade/halt", {"reason": "phone"}).status_code == 200
    r = d.money(phone, "submit", plan_id=p.id)
    assert r.status_code == 200 and "halted" in r.json()["result"] and not d.broker.orders_
    t = d.c.get("/api/v1/today").json()
    assert t["switches"]["halted"] and t["inbox"][0]["kind"] == "halted"


@pytest.mark.unit
def test_today_leads_with_the_plan_and_its_cutoff(config):
    d = Desk(config)
    p = _plan(config, d.broker, [("AAA", 1.0), ("BBB", 0.5)], day="2030-01-07")
    t = d.c.get("/api/v1/today").json()
    assert t["plan"]["id"] == p.id and t["plan"]["buys"] == 2 and t["plan"]["seconds_left"] > 0
    assert t["inbox"][0]["kind"] == "plan" and t["inbox"][0]["severity"] == "action"
    assert t["needs_you"] == 1


@pytest.mark.unit
def test_the_app_is_served_for_client_routes_and_the_v1_page_moves_to_classic(config, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root></div>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (dist / "manifest.webmanifest").write_text(json.dumps({"name": "Desk"}))
    app = create_app(config, dist=dist)
    c = TestClient(app, base_url=ORIGIN)
    assert c.get("/").text == "<div id=root></div>"
    assert c.get("/book").text == "<div id=root></div>"          # a client-side route
    assert c.get("/assets/app.js").status_code == 200
    assert c.get("/manifest.webmanifest").json()["name"] == "Desk"
    assert "DESK_TOKEN" not in c.get("/classic").text and "desk.js" in c.get("/classic").text
    assert c.get("/api/v1/nope").status_code == 404
    assert c.get("/../../etc/passwd").text == "<div id=root></div>"


@pytest.mark.unit
def test_desk_hosts_may_be_pasted_as_urls(monkeypatch):
    from tradingagents.desk.app import configured_hosts

    monkeypatch.setenv("TRADINGAGENTS_DESK_HOSTS", " https://Mac.tail1234.ts.net/ , other.ts.net:443")
    assert configured_hosts({}) == ("127.0.0.1", "localhost", "mac.tail1234.ts.net", "other.ts.net")
