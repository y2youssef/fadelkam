"""A stop signal must end in our cleanup (close Chrome, save the session cookie, tell Firebase
"stopped"), not in uvicorn re-raising the signal and the default action killing the process.
Seen 2026-11-19 under systemd: SIGTERM killed the server right after "Finished server process".
Runs a real uvicorn server in a subprocess on a random local port (no network)."""
from __future__ import annotations

import asyncio
import signal
import subprocess
import sys

import pytest

from app.config import SERVER_DIR

SCRIPT = """
import asyncio, os, signal, sys
import uvicorn
from starlette.applications import Starlette
from app.server import absorb_reraised_signals

SIG = signal.Signals[sys.argv[1]]
ABSORB = sys.argv[2] == "1"

async def main():
    server = uvicorn.Server(uvicorn.Config(Starlette(), host="127.0.0.1", port=0, log_config=None, lifespan="off"))
    if ABSORB:
        absorb_reraised_signals(server)

    async def stop_soon():
        while not server.started:
            await asyncio.sleep(0.05)
        os.kill(os.getpid(), SIG)

    task = asyncio.create_task(stop_soon())
    try:
        await server.serve()
    finally:
        await asyncio.sleep(0.1)  # the real cleanup awaits too (browser close, Firebase write)
        print("cleanup ran", flush=True)
    await task
    return 0

sys.exit(asyncio.run(main()))
"""


def run(sig: str, absorb: bool) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", SCRIPT, sig, "1" if absorb else "0"], cwd=SERVER_DIR,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("sig", ["SIGTERM", "SIGINT"])
def test_stop_signal_runs_cleanup(sig):
    r = run(sig, absorb=True)
    assert "cleanup ran" in r.stdout, r.stderr[-2000:]
    assert r.returncode == 0, r.stderr[-2000:]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_without_the_fix_sigterm_skips_cleanup():
    """Pins the uvicorn behaviour the fix exists for; if this starts failing, uvicorn changed."""
    r = run("SIGTERM", absorb=False)
    assert "cleanup ran" not in r.stdout
    assert r.returncode == -signal.SIGTERM


# ---------------------------------------------------------------- supervision of background tasks
class FakeServer:
    should_exit = False


def test_a_background_task_that_dies_stops_the_server():
    """A poller that died silently would leave the server up with a frozen reading."""
    from app.server import supervise

    async def go():
        srv, died = FakeServer(), []

        async def buggy():
            raise RuntimeError("a bug escaped every handler")
        t = asyncio.create_task(buggy(), name="poller")
        supervise(srv, [t], died)
        await asyncio.gather(t, return_exceptions=True)
        await asyncio.sleep(0)
        assert srv.should_exit and died == ["poller"]

        srv2, died2 = FakeServer(), []                       # a normal shutdown cancels the tasks
        t2 = asyncio.create_task(asyncio.sleep(30), name="cloud")
        supervise(srv2, [t2], died2)
        t2.cancel()
        await asyncio.gather(t2, return_exceptions=True)
        await asyncio.sleep(0)
        assert not srv2.should_exit and died2 == []
    asyncio.run(go())
