"""The polling loop: keep one browser alive, read the quota every `poll_interval_s`.

Run from `server/`:
    python -m app.poller --chrome            # loop forever (Ctrl-C to stop)
    python -m app.poller --chrome --once     # one reading, printed as JSON

Policy per failure class (details in docs/FAILURE_MODES.md):
    SessionExpired        -> re-login on the next tick (through the guard)
    LoginBlockedByGuard   -> sleep until the guard allows it; hard stop -> re-check every 5 min
    Challenge/Transient   -> the guard's backoff; transient fetch errors back off 1 -> 15 min
    PortalChanged/ApiError-> incident already saved; retry every 10 min (don't hammer)
    anything else         -> logged with traceback + incident; retry in 5 min
The session cookie is a browser-session cookie, so the browser is NOT restarted on purpose:
a restart would cost a login.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
from dataclasses import replace
from typing import Awaitable, Callable

from .config import Settings, load_credentials, load_settings
from .diagnostics import save_incident
from .errors import (ApiError, ChallengeRequired, LoginBlockedByGuard, PortalChanged, SessionExpired,
                     TransientError, WafBlocked, WeError)
from .login_guard import LoginGuard
from .logs import REDACT, event, get_logger, iso, setup_logging, utcnow
from .quota import Quota, parse_free_units
from .we_client import CodeProvider, WeClient

log = get_logger("poller")


class Poller:
    def __init__(self, settings: Settings, client: WeClient, *, code_provider: CodeProvider | None = None,
                 on_update: Callable[[Quota], Awaitable[None]] | None = None,
                 on_status: Callable[[dict], None] | None = None, wake: asyncio.Event | None = None) -> None:
        self.s = settings
        self.client = client
        self.code_provider = code_provider
        self.on_update = on_update
        self.on_status = on_status
        self.wake = wake or asyncio.Event()
        self.last: Quota | None = None
        self.status: dict = {"state": "starting", "since": iso()}
        self._transient_streak = 0
        self._expired_streak = 0

    def _set_status(self, state: str, **extra) -> None:
        changed = state != self.status.get("state")
        if changed:
            event(log, logging.INFO, "poller.state", state=state, **extra)
            self.status = {"state": state, "since": iso(), **extra}
        else:
            self.status.update(extra)
        if self.on_status and (changed or state != "ok"):
            self.on_status(dict(self.status))

    async def poll_once(self) -> Quota:
        await self.client.ensure_logged_in(self.code_provider)
        body = await self.client.fetch_quota_raw()
        try:
            quota = parse_free_units(body)
        except PortalChanged as e:
            e.incident = await save_incident(self.s, "quota_parse", page=self.client.page,
                                             recorder=self.client.recorder, error=e)
            raise
        self._persist(body, quota)
        await self.client.save_session_cookies()  # cheap no-op unless the cookie changed
        changed = self.last is None or quota.remaining_mb != self.last.remaining_mb
        event(log, logging.INFO, "quota.reading", remaining_mb=quota.remaining_mb, total_mb=quota.total_mb,
              renews_in_days=quota.renews_in_days, changed=changed,
              delta_mb=None if self.last is None else round(quota.remaining_mb - self.last.remaining_mb, 2))
        self.last = quota
        if self.on_update:
            await self.on_update(quota)
        return quota

    def _persist(self, raw: dict, quota: Quota) -> None:
        d = self.s.state_dir
        for name, obj in (("last_quota.json", quota.as_dict()), ("last_quota_raw.json", raw)):
            tmp = d / (name + ".tmp")
            tmp.write_text(REDACT(json.dumps(obj, ensure_ascii=False, indent=1)), encoding="utf-8")
            os.replace(tmp, d / name)

    async def tick(self) -> float:
        """One iteration; returns seconds to sleep before the next one."""
        interval = self.s.poll_interval_s
        try:
            q = await self.poll_once()
            self._transient_streak = 0
            self._expired_streak = 0
            self._set_status("ok", remaining_mb=q.remaining_mb, at=q.fetched_at)
            return interval
        except SessionExpired as e:
            # First expiry: re-login almost at once. Repeated expiries with no reading in between
            # mean we're misjudging the session, so back off instead of looping every 5 s.
            self._expired_streak += 1
            self._set_status("session_expired", error=str(e), streak=self._expired_streak)
            return 5 if self._expired_streak == 1 else min(60 * 2 ** (self._expired_streak - 2), 900)
        except LoginBlockedByGuard as e:
            self._set_status("needs_human" if e.needs_human else "login_blocked", error=str(e),
                             retry_at=iso(e.retry_at) if e.retry_at else None)
            if e.needs_human or not e.retry_at:
                return 300
            return max(30.0, (e.retry_at - utcnow()).total_seconds() + 5)
        except ChallengeRequired as e:
            self._set_status("challenge_pending", challenge=e.challenge, error=str(e))
            return 60  # guard backoff decides when the next login may happen
        except (TransientError, WafBlocked) as e:
            self._transient_streak += 1
            self._set_status("degraded", error=str(e), streak=self._transient_streak)
            return min(60 * 2 ** (self._transient_streak - 1), 900)
        except (PortalChanged, ApiError) as e:
            self._set_status("broken", error=str(e), incident=str(e.incident) if e.incident else None)
            return 600
        except WeError as e:  # LoginRejected / AccountLocked (guard already recorded them)
            self._set_status("login_failed", kind=e.kind, error=str(e))
            return 300
        except Exception as e:
            event(log, logging.ERROR, "poller.unexpected", exc_info=True)
            await save_incident(self.s, "poller_unexpected", page=self.client.page,
                                recorder=self.client.recorder, error=e)
            self._set_status("error", error=f"{type(e).__name__}: {e}")
            return 300

    def _keepalive_cap(self, delay: float) -> float:
        """While a logged-in portal page is open, its SPA logs out (server-side too) 15 min after
        the document loaded (FAILURE_MODES #25). Never sleep past keepalive_deadline_s of document
        age, so the next tick does the keepalive reload, even in the middle of a backoff."""
        try:
            if not self.client.portal_doc_live():
                return delay
            age = self.client.doc_age_s()
        except Exception:  # no page yet / browser gone: nothing to keep alive
            return delay
        cap = max(30.0, self.s.keepalive_deadline_s - age)
        if delay <= cap:
            return delay
        level = logging.DEBUG if self.status.get("state") == "ok" else logging.INFO
        event(log, level, "poller.sleep_capped", wanted_s=round(delay), capped_s=round(cap), doc_age_s=round(age),
              state=self.status.get("state"))
        return cap

    async def run_forever(self) -> None:
        event(log, logging.INFO, "poller.start", interval_s=self.s.poll_interval_s, route=self.s.quota_route)
        while True:
            delay = await self.tick()
            delay *= random.uniform(0.9, 1.1)  # jitter: don't look like a metronome
            delay = self._keepalive_cap(delay)
            event(log, logging.DEBUG, "poller.sleep", seconds=round(delay))
            try:  # a forced refresh (POST /refresh) wakes us early; the guard still gates logins
                await asyncio.wait_for(self.wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()


async def main_async(argv: list[str]) -> int:
    from .probe import terminal_code_provider

    ap = argparse.ArgumentParser(prog="python -m app.poller")
    ap.add_argument("--once", action="store_true", help="one reading, print it, exit")
    ap.add_argument("--chrome", action="store_true", help="use the installed Google Chrome")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    args = ap.parse_args(argv)
    s = load_settings()
    if args.chrome:
        s = replace(s, browser_channel="chrome")
    if args.headed:
        s = replace(s, headless=False)
    setup_logging(s.logs_dir, s.log_level)
    creds = load_credentials()
    async with WeClient(s, creds, LoginGuard(s, creds.fingerprint)) as client:
        poller = Poller(s, client, code_provider=terminal_code_provider)
        if args.once:
            try:
                q = await poller.poll_once()
            except WeError as e:
                print(json.dumps(e.as_dict(), indent=1), file=sys.stderr)
                return 1
            print(REDACT(json.dumps(q.as_dict(), indent=1)))
            print(q.summary())
            return 0
        await poller.run_forever()
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(main_async(sys.argv[1:])))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
