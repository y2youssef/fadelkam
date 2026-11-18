"""Run the whole server: one browser, the poller, and the HTTP API, in one process.

    python -m app.server [--chrome] [--headed] [--host 127.0.0.1] [--port 8080]

The API token comes from WE_API_TOKEN, or is generated once into state/api_token (read it
from there to set up the phone). The token value is never logged.
Bind defaults to 127.0.0.1: reach it through a tunnel (cloudflared / Tailscale) and never
expose it raw. Set WE_API_HOST=0.0.0.0 inside Docker.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import secrets
import sys
from dataclasses import replace

import uvicorn

from .api import create_app
from .config import Settings, load_credentials, load_settings
from .hub import Hub
from .login_guard import LoginGuard
from .logs import REDACT, event, get_logger, setup_logging
from .poller import Poller
from .we_client import WeClient

log = get_logger("server")


def ensure_token(s: Settings) -> str:
    token = os.environ.get("WE_API_TOKEN", "").strip()
    if not token:
        path = s.state_dir / "api_token"
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
        else:
            token = secrets.token_urlsafe(32)
            s.state_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(token + "\n", encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
            event(log, logging.WARNING, "api.token_generated", path=str(path),
                  hint="copy this token into the phone app")
    if len(token) < 24:
        raise SystemExit("WE_API_TOKEN must be at least 24 characters")
    REDACT.add(token, "api_token")
    return token


async def main_async(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.server")
    ap.add_argument("--chrome", action="store_true", help="use the installed Google Chrome")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--host", default=os.environ.get("WE_API_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("WE_API_PORT", "8080")))
    args = ap.parse_args(argv)

    s = load_settings()
    if args.chrome:
        s = replace(s, browser_channel="chrome")
    if args.headed:
        s = replace(s, headless=False)
    setup_logging(s.logs_dir, s.log_level)
    token = ensure_token(s)
    creds = load_credentials()
    for w in creds.warnings():
        event(log, logging.WARNING, "creds.warning", warning=w)

    hub = Hub(s)
    app = create_app(s, hub, token)
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_config=None,
                                           access_log=True, lifespan="off"))
    async with WeClient(s, creds, LoginGuard(s, creds.fingerprint)) as client:
        poller = Poller(s, client, code_provider=hub.code_provider, on_update=hub.on_quota,
                        on_status=hub.on_status, wake=hub.refresh_event)
        task = asyncio.create_task(poller.run_forever(), name="poller")
        event(log, logging.INFO, "server.start", host=args.host, port=args.port)
        try:
            await server.serve()  # returns on SIGINT/SIGTERM
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            event(log, logging.INFO, "server.stop")
    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async(sys.argv[1:])))


if __name__ == "__main__":
    main()
