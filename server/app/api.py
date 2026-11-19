"""HTTP API for the phone app (Starlette).

    GET  /healthz          no auth; liveness + poller state, no personal data
    GET  /quota            current snapshot (quota, staleness, poller status, pending challenge)
    GET  /events           Server-Sent Events: "snapshot" on connect, then quota/status/challenge;
                           a ": ping" comment every 20 s keeps tunnels/proxies from idling out
    GET  /challenge        pending captcha (or null)
    GET  /challenge/image  the captcha PNG while one is pending
    POST /challenge        {"code": "...", "id": "<challenge id, optional>"}
    POST /refresh          poll now (rate-limited)

All but /healthz need `Authorization: Bearer <token>`.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .config import Settings
from .hub import CODE_RE, Hub
from .logs import event, get_logger

log = get_logger("api")

HEARTBEAT_S = 20


def _client(request: Request) -> str:
    return request.client.host if request.client else "?"


def create_app(settings: Settings, hub: Hub, token: str, *, heartbeat_s: float = HEARTBEAT_S) -> Starlette:
    expected = f"Bearer {token}".encode()

    def authorized(request: Request) -> bool:
        ok = hmac.compare_digest(request.headers.get("authorization", "").encode(), expected)
        if not ok:
            event(log, logging.WARNING, "api.auth_failed", client=_client(request), path=request.url.path)
        return ok

    def unauthorized() -> Response:
        return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True, "state": hub.status.get("state"), "last_reading_age_s": hub.age_s(),
                             "challenge_pending": hub.challenge is not None,
                             "cloud": (hub.cloud or {}).get("state", "off")})

    async def quota(request: Request) -> Response:
        if not authorized(request):
            return unauthorized()
        return JSONResponse(hub.snapshot())

    async def events(request: Request) -> Response:
        if not authorized(request):
            return unauthorized()
        q = hub.subscribe()
        if q is None:
            return JSONResponse({"error": "too many subscribers"}, status_code=503)
        client = _client(request)

        async def stream() -> AsyncIterator[str]:
            try:
                yield "retry: 5000\n\n"  # client reconnect delay
                yield _sse(hub._seq, "snapshot", hub.snapshot())
                while True:
                    try:
                        seq, kind, data = await asyncio.wait_for(q.get(), timeout=heartbeat_s)
                        yield _sse(seq, kind, data)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                hub.unsubscribe(q)
                event(log, logging.INFO, "sse.closed", client=client)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def challenge(request: Request) -> Response:
        if not authorized(request):
            return unauthorized()
        if request.method == "GET":
            return JSONResponse({"challenge": hub.challenge})
        try:
            body = await request.json()
            code = str(body.get("code", "")).strip()
        except (ValueError, AttributeError):
            return JSONResponse({"error": "body must be JSON: {\"code\": \"...\"}"}, status_code=400)
        if not CODE_RE.match(code):
            return JSONResponse({"error": "code must be 3-12 letters/digits"}, status_code=400)
        result = hub.submit_code(code, body.get("id"))
        status = {"accepted": 200, "no_challenge": 409, "stale_id": 409}[result]
        return JSONResponse({"result": result}, status_code=status)

    async def challenge_image(request: Request) -> Response:
        if not authorized(request):
            return unauthorized()
        path = settings.state_dir / "challenge" / "captcha.png"
        if not hub.challenge or not hub.challenge.get("has_image") or not path.exists():
            return JSONResponse({"error": "no captcha pending"}, status_code=404)
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    async def refresh(request: Request) -> Response:
        if not authorized(request):
            return unauthorized()
        wait = hub.request_refresh()
        if wait:
            return JSONResponse({"error": "too soon", "retry_after_s": wait}, status_code=429,
                                headers={"Retry-After": str(int(wait) + 1)})
        return JSONResponse({"result": "polling"}, status_code=202)

    return Starlette(routes=[
        Route("/healthz", healthz),
        Route("/quota", quota),
        Route("/events", events),
        Route("/challenge", challenge, methods=["GET", "POST"]),
        Route("/challenge/image", challenge_image),
        Route("/refresh", refresh, methods=["POST"]),
    ])


def _sse(seq: int, kind: str, data: dict) -> str:
    return f"id: {seq}\nevent: {kind}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
