"""Shared state between the poller and HTTP clients, with fan-out to SSE subscribers.

The poller writes (quota readings, status changes, captcha requests) and HTTP handlers read
(snapshot, SSE stream) or answer (captcha code, forced refresh). Everything runs on one asyncio
loop, so plain attributes are enough; no locks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .logs import event, get_logger, iso, utcnow
from .quota import Quota

log = get_logger("hub")

MAX_SUBSCRIBERS = 10
QUEUE_SIZE = 50
MIN_FORCED_REFRESH_S = 30


class Hub:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.quota: dict[str, Any] | None = self._load_last_quota()
        self.status: dict[str, Any] = {"state": "starting", "since": iso()}
        self.challenge: dict[str, Any] | None = None
        self._code: asyncio.Future[str] | None = None
        self._subs: set[asyncio.Queue] = set()
        self._seq = 0
        self.refresh_event = asyncio.Event()
        self._last_forced = 0.0

    # ------------------------------------------------------------------ snapshot
    def _load_last_quota(self) -> dict[str, Any] | None:
        """Serve the last known value after a restart (it shows as stale until a new reading)."""
        p = self.s.state_dir / "last_quota.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def age_s(self) -> float | None:
        if not self.quota or not self.quota.get("fetched_at"):
            return None
        return round((utcnow() - datetime.fromisoformat(self.quota["fetched_at"])).total_seconds())

    def snapshot(self) -> dict[str, Any]:
        age = self.age_s()
        return {
            "quota": {k: v for k, v in (self.quota or {}).items() if k != "buckets"} or None,
            "buckets": (self.quota or {}).get("buckets", []),
            "age_s": age,
            # 3 missed polls (or no reading at all) = stale; the phone greys the notification out.
            "stale": age is None or age > 3 * self.s.poll_interval_s,
            "status": self.status,
            "challenge": self.challenge,
            "server_time": iso(),
        }

    # ------------------------------------------------------------------ fan-out
    def subscribe(self) -> asyncio.Queue | None:
        if len(self._subs) >= MAX_SUBSCRIBERS:
            return None
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subs.add(q)
        event(log, logging.INFO, "sse.subscribed", subscribers=len(self._subs))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)
        event(log, logging.INFO, "sse.unsubscribed", subscribers=len(self._subs))

    def publish(self, kind: str) -> None:
        self._seq += 1
        msg = (self._seq, kind, self.snapshot())
        for q in list(self._subs):
            if q.full():  # slow client: drop its oldest message rather than block the poller
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)

    # ------------------------------------------------------------------ poller callbacks
    async def on_quota(self, quota: Quota) -> None:
        self.quota = quota.as_dict()
        self.publish("quota")

    def on_status(self, status: dict[str, Any]) -> None:
        self.status = status
        self.publish("status")

    async def code_provider(self, kind: str, image: Path | None) -> str | None:
        """Asks the phone (via SSE) for the captcha code and waits for POST /challenge.
        Writing state/challenge/code.txt also works, as an operator fallback.
        The caller (WeClient) applies the timeout; cancellation clears the challenge."""
        loop = asyncio.get_running_loop()
        self._code = loop.create_future()
        self.challenge = {"id": uuid.uuid4().hex[:8], "kind": kind, "since": iso(), "has_image": image is not None}
        code_file = self.s.state_dir / "challenge" / "code.txt"
        code_file.unlink(missing_ok=True)
        event(log, logging.WARNING, "challenge.pending", id=self.challenge["id"], kind=kind)
        self.publish("challenge")
        try:
            while True:
                done, _ = await asyncio.wait({self._code}, timeout=1)
                if done:
                    return self._code.result()
                if code_file.exists():
                    code = code_file.read_text(encoding="utf-8").strip()
                    code_file.unlink(missing_ok=True)
                    return code or None
        finally:
            event(log, logging.INFO, "challenge.closed", id=self.challenge["id"])
            self.challenge = None
            self._code = None
            self.publish("challenge")

    def submit_code(self, code: str, challenge_id: str | None = None) -> str:
        """Returns 'accepted', 'no_challenge' or 'stale_id'."""
        if not self.challenge or not self._code or self._code.done():
            return "no_challenge"
        if challenge_id and challenge_id != self.challenge["id"]:
            return "stale_id"
        self._code.set_result(code)
        event(log, logging.INFO, "challenge.code_received", id=self.challenge["id"], length=len(code))
        return "accepted"

    def request_refresh(self) -> float:
        """Wake the poller now. Returns 0 if accepted, else seconds to wait."""
        wait = MIN_FORCED_REFRESH_S - (time.monotonic() - self._last_forced)
        if wait > 0:
            return round(wait, 1)
        self._last_forced = time.monotonic()
        self.refresh_event.set()
        event(log, logging.INFO, "poller.refresh_requested")
        return 0
