"""Failure taxonomy. Every way the scraper can fail maps to exactly one class, and each class
has a fixed handling policy (see docs/FAILURE_MODES.md). Unknown portal errors are never
retried blindly: they are classified conservatively as LoginRejected."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


class WeError(Exception):
    kind = "error"
    transient = False

    def __init__(self, message: str, *, error_no: str | None = None, ret_code: str | None = None,
                 incident: Path | None = None) -> None:
        super().__init__(message)
        self.error_no = error_no
        self.ret_code = ret_code
        self.incident = incident

    def as_dict(self) -> dict:
        return {"kind": self.kind, "error": str(self), "error_no": self.error_no, "ret_code": self.ret_code,
                "incident": str(self.incident) if self.incident else None}


class TransientError(WeError):
    """Network failure, timeout, 5xx. Safe to retry with backoff."""
    kind = "transient"
    transient = True


class WafBlocked(WeError):
    """F5/WAF served a block or challenge page instead of the app. Back off hard."""
    kind = "waf_blocked"
    transient = True


class PortalChanged(WeError):
    """A selector/route/response shape we depend on is gone. Needs a code fix."""
    kind = "portal_changed"


class SessionExpired(WeError):
    """Logged-in session is no longer valid; a (guarded) re-login is needed."""
    kind = "session_expired"
    transient = True


class ApiError(WeError):
    """A data call returned a non-zero retCode."""
    kind = "api_error"


class LoginRejected(WeError):
    """WE refused the login (bad credentials or an unrecognised refusal)."""
    kind = "login_rejected"


class AccountLocked(LoginRejected):
    """Too many failed attempts; WE blocks login for a while."""
    kind = "account_locked"

    def __init__(self, message: str, *, lock_minutes: int | None = None, **kw) -> None:
        super().__init__(message, **kw)
        self.lock_minutes = lock_minutes


class ChallengeRequired(WeError):
    """Login needs a human: SMS code, image/slider captcha or reCAPTCHA."""
    kind = "challenge_required"

    def __init__(self, message: str, *, challenge: str, **kw) -> None:
        super().__init__(message, **kw)
        self.challenge = challenge


class LoginBlockedByGuard(WeError):
    """Our own guard refused to submit a login (protects the account)."""
    kind = "guard_blocked"

    def __init__(self, message: str, *, retry_at: datetime | None = None, needs_human: bool = False) -> None:
        super().__init__(message)
        self.retry_at = retry_at
        self.needs_human = needs_human


# errorNo values seen in the portal bundle / live responses. Meanings are what WE returned;
# "unconfirmed" means we only know the app groups them, not the exact trigger.
KNOWN_LOGIN_ERRORS: dict[str, str] = {
    # live 2026-11-18: "You have reached the maximum number of incorrect attempts. Please try
    # again after one hour." The app lists it under CAPTCHA_INVALID_ERROR.
    "60301025072100002": "account_locked",
    # unconfirmed: sibling of the above in CAPTCHA_INVALID_ERROR.
    "60301025072100001": "login_rejected",
    # live 2026-11-19 (retCode 9999, errorMsg empty): the app's toast said "Service number or
    # password is incorrect". Cause: a misspelt label in creds.txt sent the wrong password.
    "60301023110815001": "login_rejected",
}

_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "ten": 10, "fifteen": 15,
            "twenty": 20, "thirty": 30, "half an": 0.5, "an": 1, "a": 1}


def parse_lock_minutes(msg: str) -> int | None:
    m = re.search(r"after\s+(\d+|half an|an|a|one|two|three|four|five|six|ten|fifteen|twenty|thirty)\s+(hour|minute)",
                  msg or "", re.I)
    if not m:
        return None
    n = m.group(1).lower()
    qty = float(n) if n.isdigit() else _WORDNUM.get(n, 1)
    return int(qty * 60) if m.group(2).lower() == "hour" else int(qty)


def classify_login_failure(header: dict) -> WeError:
    ret_code = str(header.get("retCode") or "")
    error_no = str(header.get("errorNo") or "") or None
    msg = str(header.get("errorMsg") or header.get("description") or "unknown login error")
    kw = {"error_no": error_no, "ret_code": ret_code}

    known = KNOWN_LOGIN_ERRORS.get(error_no or "")
    low = msg.lower()
    if known == "account_locked" or ("maximum" in low and "attempt" in low) or "locked" in low:
        return AccountLocked(msg, lock_minutes=parse_lock_minutes(msg), **kw)
    if re.search(r"verification code|sms|otp|one[- ]time", low):
        return ChallengeRequired(msg, challenge="sms", **kw)
    if "captcha" in low and known != "login_rejected":
        return ChallengeRequired(msg, challenge="captcha", **kw)
    return LoginRejected(msg, **kw)
