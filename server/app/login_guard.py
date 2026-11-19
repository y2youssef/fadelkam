"""Login guard: the only gate in front of WE's login endpoint.

Why it exists: WE locks the account after repeated failed logins (seen live: "maximum number
of incorrect attempts ... try again after one hour"), and sessions are short, so the scraper
logs in often. A bug or a wrong password must never turn into a lockout loop.

Rules (all persisted in state/login_ledger.json so a restart can't reset them):
  * An attempt is written to disk BEFORE the login is submitted; a crash mid-login still counts.
  * At least `min_login_gap_s` (10 min) between login submissions; caps per rolling hour / day.
  * WE ended a session we were using -> wait `session_lost_gap_s` (10 min) before logging in
    again: another device may have just logged in (see session_lost()).
  * AccountLocked  -> no attempts until WE's lock expires (+margin); by default also a hard stop.
  * LoginRejected  -> counts toward `max_consecutive_rejections`, then hard stop.
  * ChallengeRequired (captcha/SMS nobody answered) -> 30 min backoff; hard stop after 3 in a row.
  * Transient      -> exponential backoff (1, 2, 4 ... 30 min).
  * A hard stop clears only via `reset()` (CLI/API) or when the credentials change.
The ledger also records every session's lifetime, so we learn how long WE sessions really last.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .errors import AccountLocked, ChallengeRequired, LoginBlockedByGuard, LoginRejected, WeError
from .files import write_private
from .logs import event, get_logger, iso, utcnow

log = get_logger("guard")

_KEEP_DAYS = 14


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


class LoginGuard:
    def __init__(self, settings: Settings, creds_fingerprint: str) -> None:
        self.s = settings
        self.fp = creds_fingerprint
        self.path = settings.ledger_path
        self.d: dict[str, Any] = self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> dict[str, Any]:
        base = {"attempts": [], "sessions": [], "locked_until": None, "backoff_until": None,
                "relogin_after": None, "transient_streak": 0, "consecutive_rejections": 0,
                "consecutive_challenges": 0, "hard_stop": None}
        if self.path.exists():
            try:
                base.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                # A corrupt ledger must not silently disable the guard: fail closed.
                event(log, logging.ERROR, "guard.ledger_corrupt", path=str(self.path))
                base["hard_stop"] = {"reason": "ledger file was corrupt; inspect and reset", "since": iso(),
                                     "creds_fp": self.fp}
        return base

    def _save(self) -> None:
        cutoff = utcnow() - timedelta(days=_KEEP_DAYS)
        self.d["attempts"] = [a for a in self.d["attempts"] if _parse(a["started_at"]) >= cutoff][-500:]
        self.d["sessions"] = self.d["sessions"][-200:]
        write_private(self.path, json.dumps(self.d, indent=1))

    # ------------------------------------------------------------------ gate
    def check(self) -> None:
        """Raise LoginBlockedByGuard if a login must not be submitted right now."""
        now = utcnow()
        hs = self.d.get("hard_stop")
        if hs:
            if hs.get("creds_fp") and hs["creds_fp"] != self.fp:
                event(log, logging.WARNING, "guard.hard_stop_cleared", reason="credentials changed")
                self.d["hard_stop"] = None
                self.d["consecutive_rejections"] = 0
                self._save()
            else:
                raise LoginBlockedByGuard(f"hard stop: {hs['reason']} (since {hs['since']}); run reset after fixing",
                                          retry_at=_parse(self.d.get("locked_until")), needs_human=True)

        for key, why in (("locked_until", "WE account lock"), ("backoff_until", "transient-error backoff"),
                         ("relogin_after", "cooldown after WE ended the session (another device may have "
                                           "just logged in)")):
            until = _parse(self.d.get(key))
            if until and until > now:
                raise LoginBlockedByGuard(f"{why} until {iso(until)}", retry_at=until)

        starts = [_parse(a["started_at"]) for a in self.d["attempts"]]
        # Seen 2026-11-18: a login 45 s after a successful one was refused with WE's misleading
        # "maximum number of incorrect attempts". Logins 3.5 min+ apart were all fine.
        if starts and now - max(starts) < timedelta(seconds=self.s.min_login_gap_s):
            retry = max(starts) + timedelta(seconds=self.s.min_login_gap_s)
            raise LoginBlockedByGuard(f"min gap between logins ({self.s.min_login_gap_s}s) until {iso(retry)}",
                                      retry_at=retry)
        for window, cap in ((timedelta(hours=1), self.s.max_logins_per_hour),
                            (timedelta(days=1), self.s.max_logins_per_day)):
            recent = sorted(t for t in starts if t > now - window)
            if len(recent) >= cap:
                retry = recent[-cap] + window
                raise LoginBlockedByGuard(f"login cap reached: {len(recent)} in last {window}", retry_at=retry)

    # ------------------------------------------------------------------ bookkeeping
    def begin_attempt(self) -> str:
        att = {"id": uuid.uuid4().hex[:8], "started_at": iso(), "outcome": "pending", "creds_fp": self.fp}
        self.d["attempts"].append(att)
        self._save()
        event(log, logging.INFO, "guard.attempt_begin", attempt=att["id"],
              last_hour=self._count(timedelta(hours=1)), last_day=self._count(timedelta(days=1)))
        return att["id"]

    def end_attempt(self, attempt_id: str, outcome: str, error: WeError | None = None) -> None:
        """outcome: success | rejected | locked | challenge | transient | unknown | aborted
        (aborted = crashed before credentials were sent; counted toward caps, no penalty)"""
        now = utcnow()
        att = next((a for a in self.d["attempts"] if a["id"] == attempt_id), None)
        if att:
            att.update(outcome=outcome, ended_at=iso(now),
                       error_no=getattr(error, "error_no", None), message=str(error) if error else None)

        if outcome == "success":
            self.d.update(consecutive_rejections=0, consecutive_challenges=0, transient_streak=0, backoff_until=None)
            self.d["sessions"].append({"login_at": iso(now), "ended_at": None, "end_reason": None, "lifetime_s": None})
        elif outcome == "locked":
            mins = (getattr(error, "lock_minutes", None) or self.s.default_lockout_min) + self.s.lockout_margin_min
            self.d["locked_until"] = iso(now + timedelta(minutes=mins))
            self.d["consecutive_rejections"] += 1
            if self.s.hard_stop_on_lock:
                self._hard_stop(f"account locked by WE: {error}")
        elif outcome == "rejected":
            self.d["consecutive_rejections"] += 1
            if self.d["consecutive_rejections"] >= self.s.max_consecutive_rejections:
                self._hard_stop(f"{self.d['consecutive_rejections']} consecutive login rejections; last: {error}")
        elif outcome == "challenge":
            # Unanswered captcha/SMS: not a credential problem, but don't hammer the login either.
            self.d["consecutive_challenges"] = self.d.get("consecutive_challenges", 0) + 1
            self.d["backoff_until"] = iso(now + timedelta(minutes=self.s.challenge_backoff_min))
            if self.d["consecutive_challenges"] >= self.s.max_unanswered_challenges:
                self._hard_stop(f"{self.d['consecutive_challenges']} unanswered login challenges "
                                f"({getattr(error, 'challenge', '?')}); last: {error}")
        elif outcome in ("transient", "unknown"):
            self.d["transient_streak"] += 1
            mins = min(2 ** (self.d["transient_streak"] - 1), 30)
            self.d["backoff_until"] = iso(now + timedelta(minutes=mins))
        self._save()
        event(log, logging.INFO if outcome == "success" else logging.WARNING, "guard.attempt_end",
              attempt=attempt_id, outcome=outcome, error_no=getattr(error, "error_no", None),
              locked_until=self.d.get("locked_until"), backoff_until=self.d.get("backoff_until"),
              hard_stop=bool(self.d.get("hard_stop")))

    @staticmethod
    def outcome_for(err: BaseException) -> str:
        if isinstance(err, AccountLocked):
            return "locked"
        if isinstance(err, LoginRejected):
            return "rejected"
        if isinstance(err, ChallengeRequired):
            return "challenge"
        if isinstance(err, WeError) and err.transient:
            return "transient"
        return "unknown"

    def end_session(self, reason: str, expired: bool = True) -> float | None:
        """Close the open session record; returns its lifetime in seconds (our main metric).
        expired=False for sessions we ended ourselves (process restart) - excluded from stats."""
        open_ = [s for s in self.d["sessions"] if not s["ended_at"]]
        if not open_:
            return None
        now = utcnow()
        for s in open_[:-1]:  # older dangling records can't be measured
            s.update(ended_at=iso(now), end_reason="unknown_dangling", expired=False, lifetime_s=None)
        s = open_[-1]
        s.update(ended_at=iso(now), end_reason=reason, expired=expired,
                 lifetime_s=round((now - _parse(s["login_at"])).total_seconds()))
        self._save()
        event(log, logging.INFO if expired else logging.DEBUG, "session.ended", reason=reason, expired=expired,
              lifetime_s=s["lifetime_s"])
        return s["lifetime_s"]

    def session_lost(self, reason: str) -> None:
        """WE ended a session we were using (redirect to #/login, a session error, cookie gone).

        If the cause was another device logging in (the owner's phone, FAILURE_MODES #7), logging
        straight back in would put two logins about a minute apart: a login 45 s after another was
        answered with a one-hour lock (#1b). So no login for session_lost_gap_s, persisted so a
        restart can't skip it. The cost is ~10 min of stale data, a few times a month at most."""
        lifetime = self.end_session(reason)
        until = utcnow() + timedelta(seconds=self.s.session_lost_gap_s)
        self.d["relogin_after"] = iso(until)
        self._save()
        event(log, logging.WARNING, "guard.relogin_cooldown", reason=reason, until=iso(until),
              session_lifetime_s=lifetime)

    def resume_session(self) -> None:
        """The browser restored a live session after a restart: reopen its ledger record so its
        lifetime keeps counting from the original login."""
        if any(not s["ended_at"] for s in self.d["sessions"]) or not self.d["sessions"]:
            return
        last = self.d["sessions"][-1]
        if last.get("end_reason") == "process_restart":
            last.update(ended_at=None, end_reason=None, expired=None, lifetime_s=None)
            self._save()
            event(log, logging.INFO, "session.resumed", login_at=last["login_at"])

    def _hard_stop(self, reason: str) -> None:
        self.d["hard_stop"] = {"reason": reason, "since": iso(), "creds_fp": self.fp}
        event(log, logging.ERROR, "guard.hard_stop", reason=reason)

    def reset(self, clear_lock: bool = False) -> None:
        self.d.update(hard_stop=None, consecutive_rejections=0, consecutive_challenges=0, transient_streak=0,
                      backoff_until=None)
        if clear_lock:
            self.d["locked_until"] = None
        self._save()
        event(log, logging.WARNING, "guard.reset", clear_lock=clear_lock)

    def _count(self, window: timedelta) -> int:
        cutoff = utcnow() - window
        return sum(1 for a in self.d["attempts"] if _parse(a["started_at"]) > cutoff)

    def status(self) -> dict[str, Any]:
        done = [s["lifetime_s"] for s in self.d["sessions"]
                if s.get("lifetime_s") is not None and s.get("expired", True)]
        return {
            "hard_stop": self.d.get("hard_stop"),
            "locked_until": self.d.get("locked_until"),
            "backoff_until": self.d.get("backoff_until"),
            "relogin_after": self.d.get("relogin_after"),
            "consecutive_rejections": self.d["consecutive_rejections"],
            "consecutive_challenges": self.d.get("consecutive_challenges", 0),
            "attempts_last_hour": self._count(timedelta(hours=1)),
            "attempts_last_day": self._count(timedelta(days=1)),
            "last_attempts": self.d["attempts"][-5:],
            "session_lifetimes_s": {"count": len(done), "min": min(done, default=None),
                                    "median": sorted(done)[len(done) // 2] if done else None,
                                    "max": max(done, default=None)},
        }

    def seed_external_lock(self, until: datetime, reason: str) -> None:
        """Record a lock we learned about outside the guard (e.g. during manual recon)."""
        self.d["locked_until"] = iso(until.astimezone(timezone.utc))
        self._hard_stop(reason)
        self._save()
