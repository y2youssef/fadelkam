"""Run the whole server: one browser, the poller, and the HTTP API, in one process.

    python -m app.server [--chrome] [--headed] [--host 127.0.0.1] [--port 8080]

The phone normally reads from Firebase (app/cloud.py): it starts automatically when
state/firebase-service-account.json exists. The local HTTP API is for development on the same
network. Its token comes from WE_API_TOKEN, or is generated once into state/api_token, and the
value is never logged. Bind defaults to 127.0.0.1; never expose it raw. Set WE_API_HOST=0.0.0.0
for a phone on the same Wi-Fi, or inside Docker.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import signal
import sys

import uvicorn

from .api import create_app
from .cloud import CloudConfigError, CloudPublisher, load_cloud_config
from .config import Settings, add_browser_flags, apply_browser_flags, load_credentials, load_settings
from .files import write_private
from .hub import Hub
from .login_guard import LoginGuard
from .logs import REDACT, event, get_logger, setup_logging
from .poller import Poller
from .we_client import WeClient

log = get_logger("server")


def absorb_reraised_signals(server: uvicorn.Server) -> None:
    """uvicorn stops on SIGINT/SIGTERM and then re-raises the signal to the handler that was there
    before serve(). For SIGTERM (systemctl stop) that is the default action, which killed the
    process before our cleanup: Chrome got SIGKILLed, the session cookie wasn't saved at shutdown
    and Firebase never heard "stopped" (seen 2026-11-19 15:27 UTC, FAILURE_MODES #49). With these
    handlers installed first, the re-raise does nothing and serve() simply returns."""
    def stop(_sig: int, _frame: object) -> None:
        server.should_exit = True  # also covers a signal that lands just before uvicorn's handlers

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)


def ensure_token(s: Settings) -> str:
    token = os.environ.get("WE_API_TOKEN", "").strip()
    if not token:
        path = s.state_dir / "api_token"
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
        else:
            token = secrets.token_urlsafe(32)
            write_private(path, token + "\n")
            event(log, logging.WARNING, "api.token_generated", path=str(path),
                  hint="copy this token into the phone app")
    if len(token) < 24:
        raise SystemExit("WE_API_TOKEN must be at least 24 characters")
    REDACT.add(token, "api_token")
    return token


async def main_async(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.server")
    add_browser_flags(ap)
    ap.add_argument("--host", default=os.environ.get("WE_API_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("WE_API_PORT", "8080")))
    args = ap.parse_args(argv)

    s = apply_browser_flags(load_settings(), args)
    setup_logging(s.logs_dir, s.log_level)
    token = ensure_token(s)
    creds = load_credentials()
    for w in creds.warnings():
        event(log, logging.WARNING, "creds.warning", warning=w)

    try:  # a half-configured relay fails here, loudly, instead of leaving the phone silent
        cloud_cfg = load_cloud_config(s)
    except CloudConfigError as e:
        raise SystemExit(f"cloud: {e}")

    hub = Hub(s)
    app = create_app(s, hub, token)
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_config=None,
                                           access_log=True, lifespan="off"))
    async with WeClient(s, creds, LoginGuard(s, creds.fingerprint)) as client:
        poller = Poller(s, client, code_provider=hub.code_provider, on_update=hub.on_quota,
                        on_status=hub.on_status, wake=hub.refresh_event)
        tasks = [asyncio.create_task(poller.run_forever(), name="poller")]
        cloud = CloudPublisher(s, hub, cloud_cfg) if cloud_cfg else None
        if cloud:
            tasks.append(asyncio.create_task(cloud.run(), name="cloud"))
        event(log, logging.INFO, "server.start", host=args.host, port=args.port, cloud=bool(cloud))
        absorb_reraised_signals(server)
        try:
            await server.serve()  # returns on SIGINT/SIGTERM
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if cloud:
                await cloud.close("stopped")  # the phone shows "server stopped", not "laptop silent"
            event(log, logging.INFO, "server.stop")
    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async(sys.argv[1:])))


if __name__ == "__main__":
    main()
