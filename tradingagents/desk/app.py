"""The Desk server: FastAPI, the app, and the v1 page at /classic.

Guards, because this server can adopt screens and send orders:

- It answers only the host names it is told to: loopback, plus the Mac's
  Tailscale name when ``desk_hosts`` (or ``TRADINGAGENTS_DESK_HOSTS``) lists it.
  Any other Host is refused, which defeats DNS rebinding.
- Every action is a POST carrying ``X-Desk-Token``, a secret made at start-up.
  The app reads it from ``/api/v1/session``; the v1 page has it written in.
  Another site in the same browser can read neither (same-origin policy) and
  cannot send the header without a CORS preflight this server never grants. A
  foreign ``Origin`` is refused.
- Money actions -- approving a plan, resuming, acknowledging -- also need a
  fresh passkey assertion made for that action (``passkeys.py``). The v1 page's
  versions of them work only from the Mac itself, as the terminal does.
- The trading actions are the same functions as ``tradingagents trade`` and the
  Hermes tools, with the same refusals (halt, block, expiry, paper-only).
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from . import today as today_view, views
from .passkeys import MONEY_ACTIONS, PasskeyError, Passkeys

STATIC = Path(__file__).parent / "static"
# The built app (desk-ui/, `npm run build`), next to the package in a checkout.
DIST = Path(__file__).resolve().parents[2] / "desk-ui" / "dist"
LOCAL_HOSTS = ("127.0.0.1", "localhost")
TRADE_ACTIONS = ("submit", "halt", "resume", "ack", "reconcile")


def configured_hosts(config: dict) -> tuple[str, ...]:
    """Loopback plus the names in ``desk_hosts`` / ``TRADINGAGENTS_DESK_HOSTS``,
    each given as a bare name or pasted as a URL (``https://mac.tailnet.ts.net/``)."""
    extra = config.get("desk_hosts") or os.environ.get("TRADINGAGENTS_DESK_HOSTS", "")
    names = [_host(h.strip()) for h in (extra.split(",") if isinstance(extra, str) else extra) if h.strip()]
    return tuple(dict.fromkeys([*LOCAL_HOSTS, *(n for n in names if n)]))


def _host(value: str | None) -> str:
    return (value or "").split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0].strip("[]").lower()


def create_app(config: dict, token: str | None = None, broker_factory=None,
               allowed_hosts: tuple[str, ...] | None = None, passkeys: Passkeys | None = None,
               dist: Path | None = None) -> FastAPI:
    token = token or secrets.token_urlsafe(32)
    allowed_hosts = allowed_hosts or configured_hosts(config)
    keys = passkeys or Passkeys(config)
    dist = dist if dist is not None else DIST
    app = FastAPI(title="Trade-Agents Desk", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))

    def authorised(request: Request) -> bool:
        origin = request.headers.get("origin")
        if origin and _host(origin) not in allowed_hosts:
            return False
        return secrets.compare_digest(request.headers.get("x-desk-token", ""), token)

    def on_the_mac(request: Request) -> bool:
        """The request came straight to the loopback interface, not through a proxy."""
        forwarded = any(h in request.headers for h in ("x-forwarded-for", "tailscale-user-login", "forwarded"))
        client = request.client.host if request.client else ""
        return (not forwarded and _host(request.headers.get("host")) in LOCAL_HOSTS
                and client in ("127.0.0.1", "::1"))

    def relying_party(request: Request) -> tuple[str, str]:
        """(rp_id, origin) from the browser's Origin, which a proxy cannot rewrite."""
        origin = request.headers.get("origin", "")
        host = _host(origin)
        if not origin or host not in allowed_hosts:
            raise PasskeyError("passkeys need the page's own origin")
        return host, origin.rstrip("/")

    async def body_of(request: Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def refused(status: int, error: str) -> JSONResponse:
        return JSONResponse({"error": error}, status_code=status)

    # --- the app's API (/api/v1) -------------------------------------------------

    @app.get("/api/v1/session")
    def session(request: Request):
        rp = _host(request.headers.get("origin") or request.headers.get("host"))
        return {"token": token, "host": rp, "passkeys": len(keys.credentials(rp)),
                "on_the_mac": on_the_mac(request), "money_actions": list(MONEY_ACTIONS)}

    @app.get("/api/v1/today")
    def today():
        return today_view.today(config)

    @app.get("/api/v1/trading")
    def trading_v1():
        return views.trading(config, broker_factory)

    @app.post("/api/v1/passkeys/options")
    async def passkey_options(request: Request):
        if not authorised(request):
            return refused(403, "not authorised")
        b = await body_of(request)
        try:
            rp_id, _ = relying_party(request)
            return Response(keys.auth_options(rp_id, str(b.get("action", "")), str(b.get("subject", ""))),
                            media_type="application/json")
        except PasskeyError as exc:
            return refused(400, str(exc))

    @app.post("/api/v1/passkeys/register/options")
    async def register_options(request: Request):
        if not authorised(request):
            return refused(403, "not authorised")
        b = await body_of(request)
        try:
            rp_id, _ = relying_party(request)
            return Response(keys.registration_options(rp_id, str(b.get("code", ""))),
                            media_type="application/json")
        except PasskeyError as exc:
            return refused(400, str(exc))

    @app.post("/api/v1/passkeys/register/verify")
    async def register_verify(request: Request):
        if not authorised(request):
            return refused(403, "not authorised")
        b = await body_of(request)
        try:
            rp_id, origin = relying_party(request)
            return keys.verify_registration(rp_id, origin, b.get("credential") or {}, str(b.get("label", "")))
        except PasskeyError as exc:
            return refused(400, str(exc))

    @app.get("/api/v1/passkeys")
    def passkey_list(request: Request):
        rp = _host(request.headers.get("origin") or request.headers.get("host"))
        return {"host": rp, "passkeys": [{k: c.get(k) for k in ("id", "label", "created", "last_used")}
                                         for c in keys.credentials(rp)]}

    @app.post("/api/v1/trade/{action}")
    async def trade_v1(action: str, request: Request):
        if not authorised(request):
            return refused(403, "not authorised")
        if action not in TRADE_ACTIONS:
            return refused(404, f"unknown action {action!r}")
        b = await body_of(request)
        if action in MONEY_ACTIONS:
            subject = str(b.get("plan_id") or "") if action == "submit" else ""
            try:
                rp_id, origin = relying_party(request)
                keys.verify(rp_id, origin, b.get("assertion"), action, subject)
            except PasskeyError as exc:
                return refused(401, str(exc))
        try:
            text = await run_in_threadpool(views.trade_action, config, action, b, broker_factory)
        except ValueError as exc:
            return refused(400, str(exc))
        return {"result": text}

    # --- the v1 page's API (/api), unchanged but for the money actions ------------

    @app.get("/api/overview")
    def overview():
        return views.overview()

    @app.get("/api/lab")
    def lab():
        return views.lab(config)

    @app.get("/api/lab/{strategy}/report")
    def lab_report(strategy: str):
        text = views.lab_report(config, strategy)
        return JSONResponse({"report": text}, status_code=200 if text is not None else 404)

    @app.post("/api/lab/adopt")
    async def adopt(request: Request):
        if not authorised(request):
            return refused(403, "not authorised")
        b = await body_of(request)
        try:
            return views.adopt(config, str(b.get("strategy", "")), str(b.get("variant", "")),
                               str(b.get("reason", "")))
        except ValueError as exc:
            return refused(400, str(exc))

    @app.get("/api/trading")
    def trading():
        return views.trading(config, broker_factory)

    @app.post("/api/trade/{action}")
    async def trade(action: str, request: Request):
        if not authorised(request):
            return refused(403, "not authorised")
        if action not in TRADE_ACTIONS:
            return refused(404, f"unknown action {action!r}")
        if action in MONEY_ACTIONS and not on_the_mac(request):
            return refused(403, "away from the Mac, approve, resume and acknowledge in the app, with your passkey")
        b = await body_of(request)
        try:
            # The broker calls block; keep them off the event loop.
            text = await run_in_threadpool(views.trade_action, config, action, b, broker_factory)
        except ValueError as exc:
            return refused(400, str(exc))
        return {"result": text}

    @app.get("/api/decisions")
    def decisions(request: Request):
        q = request.query_params
        try:
            limit = max(1, min(int(q.get("limit", 50)), 500))
        except ValueError:
            limit = 50
        return {"text": views.decisions(q.get("ticker"), q.get("mandate"), q.get("since"), limit)}

    @app.get("/api/decisions/report")
    def decision_report(request: Request):
        q = request.query_params
        if not q.get("ticker") or not q.get("day"):
            return refused(400, "ticker and day")
        return {"text": views.decision_report(q["ticker"], q["day"])}

    # --- pages ---------------------------------------------------------------------

    def classic_page() -> HTMLResponse:
        html = (STATIC / "index.html").read_text().replace("{{DESK_TOKEN}}", token)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/classic")
    def classic():
        return classic_page()

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    built = (dist / "index.html").exists()
    if built and (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        """The app for every other path (its routes are client-side), or the v1
        page when the app has not been built."""
        if path.startswith("api/"):
            return refused(404, "no such endpoint")
        if not built:
            return classic_page()
        candidate = (dist / path).resolve()
        if path and re.fullmatch(r"[\w.\-/]+", path) and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})

    app.state.token = token
    app.state.passkeys = keys
    return app


def serve(config: dict, port: int = 8810, open_browser: bool = True) -> None:
    import uvicorn

    keys = Passkeys(config)
    code = keys.new_code()
    url = f"http://localhost:{port}/"
    hosts = configured_hosts(config)
    print(f"Desk: {url}  (Ctrl-C to stop)", flush=True)
    if len(hosts) > 2:
        print(f"Also answering: {', '.join(h for h in hosts if h not in LOCAL_HOSTS)}", flush=True)
    if not DIST.joinpath("index.html").exists():
        print("The app is not built (cd desk-ui && npm run build); serving the v1 page.", flush=True)
    print(f"Passkey enrolment code (15 minutes, one use): {code[:4]}-{code[4:]}", flush=True)
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    # Loopback only: Tailscale reaches it through `tailscale serve`, never directly.
    uvicorn.run(create_app(config, passkeys=keys), host="127.0.0.1", port=port, log_level="warning")
