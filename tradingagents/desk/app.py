"""The Desk server: Starlette on 127.0.0.1, one page, a small JSON API.

Guards, because this server can adopt screens and send orders:

- It listens on the loopback interface only, and answers only requests whose
  Host is ``127.0.0.1`` or ``localhost`` -- a page elsewhere cannot rebind a
  name of its own to it (DNS rebinding).
- Every action is a POST carrying ``X-Desk-Token``: a secret made at start-up and
  written into the page this server serves. Another site open in the same
  browser can neither read the page (same-origin policy) nor send the header
  without a CORS preflight this server never grants.
- The trading actions are the same functions as ``tradingagents trade`` and the
  Hermes tools, with the same refusals (halt, block, expiry, paper-only).
"""

from __future__ import annotations

import secrets
import threading
import webbrowser
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import views

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = ("127.0.0.1", "localhost")
TRADE_ACTIONS = ("submit", "halt", "resume", "ack", "reconcile")


def create_app(config: dict, token: str | None = None, broker_factory=None,
               allowed_hosts: tuple[str, ...] = LOCAL_HOSTS) -> Starlette:
    token = token or secrets.token_urlsafe(32)

    def authorised(request: Request) -> bool:
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1].split(":")[0] not in allowed_hosts:
            return False
        return secrets.compare_digest(request.headers.get("x-desk-token", ""), token)

    async def body_of(request: Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def index(request: Request) -> Response:
        html = (STATIC / "index.html").read_text().replace("{{DESK_TOKEN}}", token)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    def overview(request: Request) -> Response:
        return JSONResponse(views.overview())

    def lab(request: Request) -> Response:
        return JSONResponse(views.lab(config))

    def lab_report(request: Request) -> Response:
        text = views.lab_report(config, request.path_params["strategy"])
        return JSONResponse({"report": text}, status_code=200 if text is not None else 404)

    async def adopt(request: Request) -> Response:
        if not authorised(request):
            return JSONResponse({"error": "not authorised"}, status_code=403)
        b = await body_of(request)
        try:
            out = views.adopt(config, str(b.get("strategy", "")), str(b.get("variant", "")),
                              str(b.get("reason", "")))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(out)

    def trading(request: Request) -> Response:
        return JSONResponse(views.trading(config, broker_factory))

    async def trade(request: Request) -> Response:
        if not authorised(request):
            return JSONResponse({"error": "not authorised"}, status_code=403)
        action = request.path_params["action"]
        if action not in TRADE_ACTIONS:
            return JSONResponse({"error": f"unknown action {action!r}"}, status_code=404)
        b = await body_of(request)
        try:
            # The broker calls block; keep them off the event loop.
            text = await run_in_threadpool(views.trade_action, config, action, b, broker_factory)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"result": text})

    def decisions(request: Request) -> Response:
        q = request.query_params
        try:
            limit = max(1, min(int(q.get("limit", 50)), 500))
        except ValueError:
            limit = 50
        return JSONResponse({"text": views.decisions(q.get("ticker"), q.get("mandate"),
                                                     q.get("since"), limit)})

    def decision_report(request: Request) -> Response:
        q = request.query_params
        if not q.get("ticker") or not q.get("day"):
            return JSONResponse({"error": "ticker and day"}, status_code=400)
        return JSONResponse({"text": views.decision_report(q["ticker"], q["day"])})

    routes = [
        Route("/", index),
        Route("/api/overview", overview),
        Route("/api/lab", lab),
        Route("/api/lab/{strategy}/report", lab_report),
        Route("/api/lab/adopt", adopt, methods=["POST"]),
        Route("/api/trading", trading),
        Route("/api/trade/{action}", trade, methods=["POST"]),
        Route("/api/decisions", decisions),
        Route("/api/decisions/report", decision_report),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    return Starlette(routes=routes,
                     middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))])


def serve(config: dict, port: int = 8765, open_browser: bool = True) -> None:
    import uvicorn

    url = f"http://127.0.0.1:{port}/"
    print(f"Desk: {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(config), host="127.0.0.1", port=port, log_level="warning")
