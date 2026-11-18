"""Runtime settings (env-driven) and credential loading.

Every knob is an env var so the same code runs locally and in Docker. Credentials come from
WE_PHONE / WE_PASSWORD (Docker) or a creds file (local dev). They are never logged; the
password field is excluded from repr and both values are registered with the log redactor.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SERVER_DIR.parent


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    v = _env(name)
    return default if v is None else int(v)


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    base_url: str = "https://my.te.eg/echannel/"
    headless: bool = True
    # "chrome" = use the locally installed Google Chrome; None = Playwright's bundled Chromium (Docker).
    browser_channel: str | None = None
    nav_timeout_ms: int = 45_000
    api_timeout_ms: int = 30_000
    # SPA route that calls queryFreeUnit on every visit. Live 2026-11-18: /accountoverview
    # re-fetches each visit (~10 calls, <1 s). /overview fetches only once per page load, so
    # later visits return nothing.
    quota_route: str = "/accountoverview"
    # Cheap route visited first when we're already on quota_route, so the page re-mounts and
    # re-fetches without a full reload (/usage made 2 calls). Fallback is a full reload.
    refresh_hop_route: str = "/usage"
    route_wait_ms: int = 15_000  # the call comes within ~1 s when it comes at all
    # The SPA logs itself out 15 min after a document load (client-side timer). Reload before that.
    # 11 min + one poll interval (<=3 min) stays under 15.
    doc_max_age_s: int = 11 * 60
    poll_interval_s: int = 180

    # ---- login guard (protects the account from lockouts) ----
    min_login_gap_s: int = 600           # a login 45 s after another was refused as "max attempts"
    max_logins_per_hour: int = 4
    max_logins_per_day: int = 30
    lockout_margin_min: int = 5          # added on top of the lock duration WE reports
    default_lockout_min: int = 60        # used when WE's message has no parsable duration
    max_consecutive_rejections: int = 2  # then hard-stop until a human resets
    hard_stop_on_lock: bool = True       # a lockout means something is wrong -> require a human
    challenge_backoff_min: int = 30      # after a captcha/SMS challenge nobody answered
    max_unanswered_challenges: int = 3   # then hard-stop
    max_captcha_rounds: int = 2          # wrong captcha codes tolerated within one login attempt
    challenge_timeout_s: int = 600       # how long to wait for a human to supply a code

    incident_retention: int = 50
    log_level: str = "INFO"

    @property
    def profile_dir(self) -> Path:
        return self.state_dir / "profile"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def incidents_dir(self) -> Path:
        return self.state_dir / "incidents"

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "login_ledger.json"

    @property
    def login_url(self) -> str:
        return self.base_url + "#/login"

    def route_url(self, route: str) -> str:
        return self.base_url + "#" + (route if route.startswith("/") else "/" + route)


def load_settings() -> Settings:
    state = Path(_env("WE_STATE_DIR", str(SERVER_DIR / "state"))).resolve()
    return Settings(
        state_dir=state,
        headless=_env_bool("WE_HEADLESS", True),
        browser_channel=_env("WE_BROWSER_CHANNEL"),
        nav_timeout_ms=_env_int("WE_NAV_TIMEOUT_MS", 45_000),
        api_timeout_ms=_env_int("WE_API_TIMEOUT_MS", 30_000),
        quota_route=_env("WE_QUOTA_ROUTE", "/accountoverview"),
        refresh_hop_route=_env("WE_REFRESH_HOP_ROUTE", "/usage"),
        poll_interval_s=_env_int("WE_POLL_INTERVAL_S", 180),
        min_login_gap_s=_env_int("WE_MIN_LOGIN_GAP_S", 600),
        max_logins_per_hour=_env_int("WE_MAX_LOGINS_PER_HOUR", 4),
        max_logins_per_day=_env_int("WE_MAX_LOGINS_PER_DAY", 30),
        max_consecutive_rejections=_env_int("WE_MAX_CONSECUTIVE_REJECTIONS", 2),
        hard_stop_on_lock=_env_bool("WE_HARD_STOP_ON_LOCK", True),
        incident_retention=_env_int("WE_INCIDENT_RETENTION", 50),
        log_level=_env("WE_LOG_LEVEL", "INFO"),
    )


# --------------------------------------------------------------------------- credentials

_PHONE_KEYS = {"phone", "number", "service", "service_number", "msisdn", "user", "username", "login"}
_PASS_KEYS = {"password", "pass", "pwd"}


@dataclass(frozen=True)
class Credentials:
    phone: str
    password: str = field(repr=False)

    @property
    def fingerprint(self) -> str:
        """Stable, non-reversible id of this credential pair (detects 'creds were changed')."""
        return hashlib.sha256(f"{self.phone}\x00{self.password}".encode()).hexdigest()[:16]

    def warnings(self) -> list[str]:
        out = []
        digits = self.phone.isdigit()
        if not (digits and len(self.phone) == 11 and self.phone.startswith("01")):
            out.append("service number is not an 11-digit Egyptian mobile number (01XXXXXXXXX)")
        elif not self.phone.startswith("015"):
            out.append("service number prefix is not WE's native 015 (fine only if the line was ported to WE)")
        return out


def _parse_creds_text(text: str) -> Credentials:
    lines = [ln.strip() for ln in text.lstrip("﻿").splitlines() if ln.strip()]
    phone = password = None
    raw = []
    for ln in lines:
        key, sep, val = ln.partition(":")
        k = key.strip().lower().replace(" ", "_")
        if sep and k in _PHONE_KEYS and phone is None:
            phone = val.strip()
        elif sep and k in _PASS_KEYS and password is None:
            password = val.strip()
        else:
            raw.append(ln)
    if phone is None and raw:
        phone = raw.pop(0)
    if password is None and raw:
        password = raw.pop(0)
    if not phone or not password:
        raise ValueError("credentials incomplete: need a phone/service number and a password")
    phone = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if phone.startswith("+20"):
        phone = "0" + phone[3:]
    return Credentials(phone=phone, password=password)


def load_credentials() -> Credentials:
    phone, password = _env("WE_PHONE"), _env("WE_PASSWORD")
    if phone and password:
        return _parse_creds_text(f"phone: {phone}\npassword: {password}")
    path = Path(_env("WE_CREDS_FILE", str(PROJECT_DIR / "creds.txt")))
    if not path.exists():
        raise FileNotFoundError(f"no WE_PHONE/WE_PASSWORD env and no creds file at {path}")
    return _parse_creds_text(path.read_text(encoding="utf-8"))
