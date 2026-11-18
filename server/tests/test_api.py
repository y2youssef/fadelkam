"""API tests: auth, snapshot, captcha round-trip, refresh rate-limit, and a real SSE stream."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from app.api import create_app
from app.config import Settings
from app.hub import Hub
from app.quota import parse_free_units

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REAL = json.loads((Path(__file__).parent / "fixtures" / "free_unit_2026-11-18.json").read_text(encoding="utf-8"))


@pytest.fixture
def settings(tmp_path):
    return Settings(state_dir=tmp_path)


def run(coro):
    return asyncio.run(coro)


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_auth_and_health(settings):
    async def go():
        async with client_for(create_app(settings, Hub(settings), TOKEN)) as c:
            h = await c.get("/healthz")
            assert h.status_code == 200 and "quota" not in h.json()          # no personal data
            assert (await c.get("/quota")).status_code == 401
            assert (await c.get("/quota", headers={"Authorization": "Bearer nope"})).status_code == 401
            assert (await c.get("/events")).status_code == 401
            r = await c.get("/quota", headers=AUTH)
            assert r.status_code == 200 and r.json()["stale"] is True and r.json()["quota"] is None
    run(go())


def test_quota_snapshot_after_reading(settings):
    async def go():
        hub = Hub(settings)
        await hub.on_quota(parse_free_units(REAL))
        async with client_for(create_app(settings, hub, TOKEN)) as c:
            snap = (await c.get("/quota", headers=AUTH)).json()
            assert snap["quota"]["remaining_mb"] == 3946.78 and snap["stale"] is False
            assert len(snap["buckets"]) == 2
    run(go())


def test_last_quota_survives_restart(settings):
    (settings.state_dir / "last_quota.json").write_text(json.dumps(parse_free_units(REAL).as_dict()))
    assert Hub(settings).snapshot()["quota"]["remaining_mb"] == 3946.78


def test_captcha_round_trip(settings):
    async def go():
        hub = Hub(settings)
        img = settings.state_dir / "challenge" / "captcha.png"
        img.parent.mkdir(parents=True)
        img.write_bytes(b"\x89PNG fake")
        async with client_for(create_app(settings, hub, TOKEN)) as c:
            assert (await c.post("/challenge", json={"code": "abc12"}, headers=AUTH)).status_code == 409
            assert (await c.get("/challenge/image", headers=AUTH)).status_code == 404
            waiter = asyncio.create_task(hub.code_provider("image_captcha", img))
            await asyncio.sleep(0.05)
            ch = (await c.get("/challenge", headers=AUTH)).json()["challenge"]
            assert ch["kind"] == "image_captcha" and ch["has_image"]
            assert (await c.get("/challenge/image", headers=AUTH)).content == b"\x89PNG fake"
            assert (await c.post("/challenge", json={"code": "no spaces!"}, headers=AUTH)).status_code == 400
            r = await c.post("/challenge", json={"code": "vw2aE", "id": "old"}, headers=AUTH)
            assert r.status_code == 409 and r.json()["result"] == "stale_id"
            r = await c.post("/challenge", json={"code": "vw2aE", "id": ch["id"]}, headers=AUTH)
            assert r.status_code == 200
            assert await waiter == "vw2aE"
            assert (await c.get("/challenge", headers=AUTH)).json()["challenge"] is None
    run(go())


def test_captcha_file_fallback_and_timeout_cleanup(settings):
    async def go():
        hub = Hub(settings)
        (settings.state_dir / "challenge").mkdir(parents=True)
        waiter = asyncio.create_task(hub.code_provider("image_captcha", None))
        await asyncio.sleep(0.1)
        (settings.state_dir / "challenge" / "code.txt").write_text("mrWw4")
        assert await waiter == "mrWw4"
        with pytest.raises(asyncio.TimeoutError):  # nobody answers -> WeClient's wait_for cancels us
            await asyncio.wait_for(hub.code_provider("image_captcha", None), timeout=0.2)
        assert hub.challenge is None
    run(go())


def test_refresh_rate_limited(settings):
    async def go():
        hub = Hub(settings)
        async with client_for(create_app(settings, hub, TOKEN)) as c:
            assert (await c.post("/refresh", headers=AUTH)).status_code == 202
            assert hub.refresh_event.is_set()
            r = await c.post("/refresh", headers=AUTH)
            assert r.status_code == 429 and "Retry-After" in r.headers
    run(go())


def test_sse_stream_snapshot_then_heartbeat(settings):
    """Real uvicorn server: the stream must start with a snapshot and keep sending heartbeats."""
    hub = Hub(settings)
    app = create_app(settings, hub, TOKEN, heartbeat_s=0.2)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]
        lines = []
        with httpx.stream("GET", f"http://127.0.0.1:{port}/events", headers=AUTH, timeout=5) as r:
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
            for line in r.iter_lines():
                lines.append(line)
                if line == ": ping":
                    break
        assert "event: snapshot" in lines
        data = json.loads(next(l for l in lines if l.startswith("data: "))[6:])
        assert set(data) >= {"quota", "stale", "status", "challenge"}
    finally:
        server.should_exit = True
        thread.join(timeout=5)
