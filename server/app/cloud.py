"""Cloud relay: the laptop pushes readings to Firebase, and the phone reads them from there.

Why: the laptop moves between home Wi-Fi networks and can't accept incoming connections, and the
phone (Samsung, aggressive background killing) is only woken reliably by FCM. So:

    poller -> Hub -> CloudPublisher --HTTPS--> Realtime Database   /state, /challenge
                                    --HTTPS--> FCM (captcha, needs-attention, reading changes)
    phone (Firebase SDK) --> /inbox/<uid>/{answer,refresh,hello} --stream--> CloudPublisher -> Hub

The laptop only makes outbound HTTPS calls. WE only ever sees the laptop's home IP.

What goes up is a fixed whitelist (`cloud_state`): numbers, offer name, dates and the poller state.
Never the phone number, cookies, raw portal responses or local paths. The captcha image is
uploaded while a captcha is pending and deleted afterwards.

Auth: a service-account key (state/firebase-service-account.json, git-ignored) signs a JWT that is
exchanged for a 1 h OAuth token (RTDB + FCM scopes). That admin access bypasses the security
rules (firebase/database.rules.json). The rules let a paired phone read /state and /challenge
and write only its own /inbox and /devices entries.

CLI: `python -m app.cloud check|deploy-rules|pair|devices|revoke|test-push` (code in cloud_cli.py).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .config import PROJECT_DIR, Settings
from .hub import CODE_RE, Hub
from .logs import REDACT, event, get_logger, iso, utcnow

log = get_logger("cloud")

SERVER_TS = {".sv": "timestamp"}
GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
SCOPES = ("https://www.googleapis.com/auth/firebase.database",
          "https://www.googleapis.com/auth/userinfo.email",
          "https://www.googleapis.com/auth/firebase.messaging")
TRUSTED_DB_SUFFIXES = (".firebasedatabase.app", ".firebaseio.com")
FCM_ENDPOINT = "https://fcm.googleapis.com"
RULES_FILE = PROJECT_DIR / "firebase" / "database.rules.json"

QUOTA_KEYS = ("remaining_mb", "total_mb", "used_mb", "offer", "expires_at", "renews_in_days", "fetched_at")
STATUS_KEYS = ("state", "since", "retry_at", "streak", "kind", "error")
ATTENTION = {"needs_human", "login_failed", "broken", "error"}  # worth a notification
URGENT = {"needs_human", "login_failed"}                         # ...a high-priority one
UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
PATH_RE = re.compile(r"[A-Za-z]:\\[^\s'\"]*|/(?:home|Users|root|tmp|var)/[^\s'\"]*")
MAX_IMAGE_BYTES = 300_000
_UNKNOWN = object()


class CloudError(Exception):
    """A cloud call failed. The publisher retries with backoff, and the scraper is unaffected."""


class CloudConfigError(CloudError):
    """Setup problem: fix the key file or env (see server/README.md, "Cloud")."""


class CloudHttpError(CloudError):
    def __init__(self, what: str, status: int, body: str) -> None:
        hint = " (revoked key or wrong system clock?)" if what == "token exchange" and status in (400, 401) else ""
        super().__init__(f"{what}: HTTP {status} {short(body)}{hint}")
        self.status = status


def short(text: str, n: int = 200) -> str:
    """Redacted, single-line, path-free, truncated. Safe for logs and for the cloud."""
    text = PATH_RE.sub("<path>", REDACT(" ".join(str(text).split())))
    return text if len(text) <= n else text[: n - 1] + "…"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def backoff(fails: int, lo: float, hi: float) -> float:
    return min(lo * 2 ** max(fails - 1, 0), hi)


# --------------------------------------------------------------------------- config
@dataclass(frozen=True)
class CloudConfig:
    sa_path: Path
    db_url: str
    project_id: str
    heartbeat_s: int = 300
    fcm_quota_min_gap_s: int = 45    # at most one "reading changed" push per 45 s (one per poll at 45 s)
    devices_cache_s: int = 300
    inbox_max_age_s: int = 900       # ignore phone messages older than this (laptop was off)
    retry_min_s: float = 2.0
    retry_max_s: float = 300.0
    stream_read_timeout_s: float = 90.0  # RTDB sends keep-alive every ~30 s


def load_service_account(path: Path) -> dict[str, Any]:
    try:
        sa = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CloudConfigError(f"can't read service-account key {path}: {type(e).__name__}") from e
    missing = [k for k in ("type", "project_id", "private_key", "client_email") if not sa.get(k)]
    if sa.get("type") != "service_account" or missing:
        raise CloudConfigError(f"{path} is not a Firebase service-account key (missing: {missing or 'type'})")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        event(log, logging.WARNING, "cloud.sa_permissions", path=str(path), hint=f"chmod 600 {path}")
    REDACT.add(sa.get("private_key_id"), "sa_key_id")
    return sa


def load_cloud_config(s: Settings) -> CloudConfig | None:
    """None = cloud off (no key file, or WE_CLOUD=0). Raises CloudConfigError for a broken setup,
    so a half-configured relay fails loudly at startup instead of leaving the phone silent."""
    mode = (os.environ.get("WE_CLOUD") or "auto").strip().lower()
    if mode in ("0", "off", "false", "no"):
        event(log, logging.INFO, "cloud.disabled", reason="WE_CLOUD=0")
        return None
    sa_path = Path(os.environ.get("WE_FIREBASE_SA") or s.state_dir / "firebase-service-account.json")
    if not sa_path.exists():
        if mode in ("1", "on", "true", "yes"):
            raise CloudConfigError(f"WE_CLOUD=1 but there is no service-account key at {sa_path}")
        event(log, logging.INFO, "cloud.disabled", reason="no service-account key", path=str(sa_path))
        return None
    sa = load_service_account(sa_path)
    # Default instance URL when the database is created in europe-west1 (closest to Egypt).
    db_url = (os.environ.get("WE_FIREBASE_DB_URL")
              or f"https://{sa['project_id']}-default-rtdb.europe-west1.firebasedatabase.app").rstrip("/")
    u = httpx.URL(db_url)
    if u.scheme != "https" or not u.host.endswith(TRUSTED_DB_SUFFIXES) or u.path not in ("", "/"):
        raise CloudConfigError(f"WE_FIREBASE_DB_URL doesn't look like a Realtime Database URL: {db_url}")
    return CloudConfig(sa_path=sa_path, db_url=db_url, project_id=sa["project_id"],
                       heartbeat_s=int(os.environ.get("WE_CLOUD_HEARTBEAT_S") or 300))


# --------------------------------------------------------------------------- Google auth
class GoogleAuth:
    """Service-account JWT -> OAuth access token (RFC 7523), cached until 5 min before expiry."""

    def __init__(self, sa: dict[str, Any], http: httpx.AsyncClient, clock: Callable[[], float] = time.time) -> None:
        self.email = sa["client_email"]
        self.token_uri = sa.get("token_uri") or "https://oauth2.googleapis.com/token"
        self.key_id = sa.get("private_key_id")
        self._key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
        self.http = http
        self.clock = clock
        self._token: str | None = None
        self._exp = 0.0
        self._lock = asyncio.Lock()

    def assertion(self) -> str:
        now = int(self.clock())
        header = {"alg": "RS256", "typ": "JWT", **({"kid": self.key_id} if self.key_id else {})}
        claims = {"iss": self.email, "scope": " ".join(SCOPES), "aud": self.token_uri, "iat": now, "exp": now + 3600}
        signing_input = ".".join(_b64url(json.dumps(x, separators=(",", ":")).encode()) for x in (header, claims))
        sig = self._key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input}.{_b64url(sig)}"

    def invalidate(self) -> None:
        self._token = None

    async def token(self) -> str:
        async with self._lock:
            if self._token and self.clock() < self._exp - 300:
                return self._token
            try:
                r = await self.http.post(self.token_uri, data={"grant_type": GRANT, "assertion": self.assertion()},
                                         timeout=20)
            except httpx.HTTPError as e:
                raise CloudError(f"token exchange: {type(e).__name__}: {short(str(e))}") from e
            if r.status_code != 200:
                raise CloudHttpError("token exchange", r.status_code, r.text)
            body = r.json()
            self._token = body["access_token"]
            self._exp = self.clock() + int(body.get("expires_in", 3600))
            REDACT.add(self._token, "oauth_token")
            event(log, logging.DEBUG, "cloud.token_refreshed", expires_in_s=int(body.get("expires_in", 3600)))
            return self._token


# --------------------------------------------------------------------------- Realtime Database (REST)
def _trusted_redirect(r: httpx.Response) -> str:
    """RTDB redirects to other servers of the same database. The token only follows to those."""
    target = r.url.join(r.headers.get("location", ""))
    if target.scheme != "https" or not target.host.endswith(TRUSTED_DB_SUFFIXES):
        raise CloudError(f"refusing redirect to {target.host!r}")
    return str(target)


async def parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, Any]]:
    ev: str | None = None
    data: list[str] = []
    async for line in lines:
        if not line:
            if ev is not None:
                raw = "\n".join(data)
                try:
                    payload = json.loads(raw) if raw else None
                except ValueError:
                    payload = raw
                yield ev, payload
            ev, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


class Rtdb:
    def __init__(self, db_url: str, auth: GoogleAuth, http: httpx.AsyncClient, read_timeout: float = 90.0) -> None:
        self.base = db_url.rstrip("/")
        self.auth = auth
        self.http = http
        self.read_timeout = read_timeout

    def url(self, path: str) -> str:
        return f"{self.base}/{path.strip('/')}.json"

    async def request(self, method: str, path: str, body: Any = _UNKNOWN, *, silent: bool = False) -> Any:
        url, params = self.url(path), ({"print": "silent"} if silent else None)
        content = None if body is _UNKNOWN else json.dumps(body, ensure_ascii=False)
        retried_auth = False
        for _ in range(4):
            headers = {"Authorization": f"Bearer {await self.auth.token()}"}
            if content is not None:
                headers["Content-Type"] = "application/json"
            try:
                r = await self.http.request(method, url, params=params, content=content, headers=headers, timeout=20)
            except httpx.HTTPError as e:
                raise CloudError(f"{method} /{path}: {type(e).__name__}: {short(str(e))}") from e
            if r.is_redirect:
                url, params = _trusted_redirect(r), None
                continue
            if r.status_code == 401 and not retried_auth:
                self.auth.invalidate()
                retried_auth = True
                continue
            if r.status_code >= 400:
                raise CloudHttpError(f"{method} /{path}", r.status_code, r.text)
            if not r.content:
                return None
            try:
                return r.json()
            except ValueError:  # e.g. rules edited in the console, with comments
                return r.text
        raise CloudError(f"{method} /{path}: too many redirects")

    async def get(self, path: str) -> Any:
        return await self.request("GET", path)

    async def put(self, path: str, value: Any) -> None:
        await self.request("PUT", path, value, silent=True)

    async def patch(self, path: str, value: dict[str, Any]) -> None:
        await self.request("PATCH", path, value, silent=True)

    async def delete(self, path: str) -> None:
        await self.request("DELETE", path, silent=True)

    async def stream(self, path: str) -> AsyncIterator[tuple[str, Any]]:
        """Server-Sent Events for `path`: ("put"|"patch", {"path", "data"}), "keep-alive",
        "cancel", "auth_revoked". Ends when the server closes the stream."""
        url = self.url(path)
        timeout = httpx.Timeout(20.0, read=self.read_timeout)
        for _ in range(4):
            headers = {"Authorization": f"Bearer {await self.auth.token()}", "Accept": "text/event-stream"}
            try:
                async with self.http.stream("GET", url, headers=headers, timeout=timeout) as r:
                    if r.is_redirect:
                        url = _trusted_redirect(r)
                        continue
                    if r.status_code != 200:
                        await r.aread()
                        if r.status_code == 401:
                            self.auth.invalidate()
                        raise CloudHttpError(f"stream /{path}", r.status_code, r.text)
                    async for item in parse_sse(r.aiter_lines()):
                        yield item
                    return
            except httpx.HTTPError as e:
                raise CloudError(f"stream /{path}: {type(e).__name__}: {short(str(e))}") from e
        raise CloudError(f"stream /{path}: too many redirects")


# --------------------------------------------------------------------------- FCM (HTTP v1)
class Fcm:
    def __init__(self, project_id: str, auth: GoogleAuth, http: httpx.AsyncClient, endpoint: str = FCM_ENDPOINT) -> None:
        self.url = f"{endpoint}/v1/projects/{project_id}/messages:send"
        self.auth = auth
        self.http = http

    async def send(self, target: dict[str, str], data: dict[str, str], *, high: bool, collapse_key: str,
                   ttl_s: int) -> str:
        """target: {"fid": <Firebase installation ID>} (firebase-messaging 25.1.0+, what the app
        registers) or {"token": <registration token>} (deprecated, still accepted).
        'ok' or 'unregistered' (dead id/token: forget it). Other failures raise CloudError."""
        msg = {"message": {**target, "data": data,
                           "android": {"priority": "HIGH" if high else "NORMAL", "collapse_key": collapse_key,
                                       "ttl": f"{int(ttl_s)}s"}}}
        retried_auth = False
        while True:
            try:
                r = await self.http.post(self.url, json=msg, timeout=20,
                                         headers={"Authorization": f"Bearer {await self.auth.token()}"})
            except httpx.HTTPError as e:
                raise CloudError(f"FCM send: {type(e).__name__}: {short(str(e))}") from e
            if r.status_code == 200:
                return "ok"
            if r.status_code == 401 and not retried_auth:
                self.auth.invalidate()
                retried_auth = True
                continue
            if r.status_code == 404 or "UNREGISTERED" in r.text.upper() or (
                    r.status_code == 400 and "registration token" in r.text.lower()):
                return "unregistered"
            raise CloudHttpError("FCM send", r.status_code, r.text)


# --------------------------------------------------------------------------- what goes up
def cloud_state(snap: dict[str, Any]) -> dict[str, Any]:
    """The whitelist. Anything not named here stays on the laptop."""
    q = snap.get("quota") or None
    st = snap.get("status") or {}
    status = {k: st[k] for k in STATUS_KEYS if st.get(k) is not None}
    if "error" in status:
        status["error"] = short(status["error"], 160)
    status.setdefault("state", "unknown")
    return {"quota": {k: q.get(k) for k in QUOTA_KEYS} if q else None, "status": status}


def _changes(state: dict[str, Any] | None) -> Any:
    """What counts as a change worth a write. A new reading time alone doesn't: the heartbeat
    (every heartbeat_s) carries it, so the phone's "updated N min ago" lags by at most that,
    and a phone listening on mobile data downloads ~300 writes a day instead of ~1000."""
    if not state:
        return None
    q = dict(state["quota"] or {})
    q.pop("fetched_at", None)
    return q, state["status"]


def inbox_items(ev: str, payload: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """Flatten an /inbox stream event into (uid, kind, value) items. value None = deleted."""
    parts = [p for p in str(payload.get("path", "/")).split("/") if p]
    data = payload.get("data")
    out: list[tuple[str, str, Any]] = []

    def walk(prefix: list[str], value: Any) -> None:
        if len(prefix) == 0:
            for uid, v in (value or {}).items() if isinstance(value, dict) else ():
                walk([uid], v)
        elif len(prefix) == 1:
            if value is None:
                return
            for kind, v in value.items() if isinstance(value, dict) else ():
                out.append((prefix[0], kind, v))
        elif len(prefix) == 2:
            out.append((prefix[0], prefix[1], value))
        elif len(prefix) == 3 and prefix[1] == "session":  # one event under /inbox/<uid>/session/<id>
            out.append((prefix[0], "session", {prefix[2]: value}))
        else:  # a sub-field changed: report the item as malformed rather than guess
            out.append((prefix[0], prefix[1], {"_partial": True}))

    if ev == "patch" and isinstance(data, dict):
        for k, v in data.items():
            walk(parts + [k], v)
    else:
        walk(parts, data)
    return out


# --------------------------------------------------------------------------- the publisher
class CloudPublisher:
    """Mirrors the hub to Firebase and relays the phone's answers back. Runs next to the poller and
    never blocks it: every failure is logged and retried with backoff."""

    def __init__(self, settings: Settings, hub: Hub, cfg: CloudConfig, *, http: httpx.AsyncClient | None = None,
                 auth: GoogleAuth | None = None, fcm_endpoint: str = FCM_ENDPOINT) -> None:
        self.s, self.hub, self.cfg = settings, hub, cfg
        self._own_http = http is None
        self.http = http or httpx.AsyncClient()
        self.auth = auth or GoogleAuth(load_service_account(cfg.sa_path), self.http)
        self.db = Rtdb(cfg.db_url, self.auth, self.http, read_timeout=cfg.stream_read_timeout_s)
        self.fcm = Fcm(cfg.project_id, self.auth, self.http, endpoint=fcm_endpoint)
        self._dirty = asyncio.Event()
        self._force_write = True             # first sync always writes (and announces we're back)
        self._last_state: dict[str, Any] | None = None
        self._last_write = 0.0
        self._published_challenge: Any = _UNKNOWN  # unknown at start: clears a leftover from a crash
        self._announced: set[str] = set()
        self._pushed_state: str | None = None
        self._pushed_mb: float | None = None
        self._pushed_at = -1e9
        self._push_fails = 0
        self._wake: asyncio.TimerHandle | None = None
        self._devices: dict[str, str] | None = None
        self._devices_at = 0.0
        self._usage_queued = False
        self.health: dict[str, Any] = {"state": "starting", "since": iso()}
        hub.cloud = self.health
        hub.add_listener(lambda kind: self._dirty.set())

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        event(log, logging.INFO, "cloud.start", db_host=httpx.URL(self.cfg.db_url).host,
              project=self.cfg.project_id, heartbeat_s=self.cfg.heartbeat_s)
        tasks = [asyncio.create_task(self._forever(n, f), name=f"cloud.{n}") for n, f in
                 (("sync", self._sync_loop), ("heartbeat", self._heartbeat_loop), ("inbox", self._inbox_loop))]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self, final_state: str | None = "stopped") -> None:
        """Tell the phone the server stopped on purpose (vs. a laptop that just went quiet)."""
        if final_state:
            try:
                await asyncio.wait_for(self.db.patch("state", {"status": {"state": final_state, "since": iso()},
                                                               "heartbeat_at": SERVER_TS}), 5)
                event(log, logging.INFO, "cloud.final_state_written", state=final_state)
            except (CloudError, asyncio.TimeoutError) as e:
                event(log, logging.WARNING, "cloud.final_state_failed", error=short(str(e)))
            # The phone keeps no connection open (battery); it learns everything from pushes.
            try:
                await asyncio.wait_for(self._push_all({"type": "status", "state": final_state}, high=False,
                                                      collapse="status", ttl_s=6 * 3600), 5)
            except (CloudError, asyncio.TimeoutError) as e:
                event(log, logging.WARNING, "cloud.final_push_failed", error=short(str(e)))
        if self._wake:
            self._wake.cancel()
        if self._own_http:
            await self.http.aclose()

    async def _forever(self, name: str, fn: Callable[[], Any]) -> None:
        fails = 0
        while True:
            try:
                await fn()
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # a bug here must never take the scraper down
                fails += 1
                delay = backoff(fails, self.cfg.retry_min_s, self.cfg.retry_max_s)
                event(log, logging.ERROR, "cloud.task_crashed", task=name, retry_in_s=delay, exc_info=True)
                await asyncio.sleep(delay)

    def _set_health(self, state: str, **extra: Any) -> None:
        if state != self.health.get("state"):
            self.health.clear()
            self.health.update({"state": state, "since": iso()})
        self.health.update(extra)

    def _wake_in(self, seconds: float) -> None:
        if self._wake:
            self._wake.cancel()
        self._wake = asyncio.get_running_loop().call_later(max(seconds, 0.0), self._dirty.set)

    # ------------------------------------------------------------------ hub -> cloud
    async def _sync_loop(self) -> None:
        fails = 0
        self._dirty.set()
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            try:
                await self.sync_db()
            except CloudError as e:
                fails += 1
                delay = backoff(fails, self.cfg.retry_min_s, self.cfg.retry_max_s)
                self._set_health("error", error=short(str(e)))
                event(log, logging.WARNING, "cloud.sync_failed", error=short(str(e)), retry_in_s=delay, streak=fails)
                await asyncio.sleep(delay)
                self._dirty.set()
                continue
            if fails:
                event(log, logging.INFO, "cloud.sync_recovered", after_failures=fails)
            fails = 0
            self._set_health("ok", last_sync=iso())
            await self.sync_push()

    async def sync_db(self) -> None:
        snap = self.hub.snapshot()
        state = cloud_state(snap)
        if self._force_write or _changes(state) != _changes(self._last_state):
            await self.db.patch("state", {**state, "heartbeat_at": SERVER_TS, "heartbeat_s": self.cfg.heartbeat_s,
                                          "poll_interval_s": self.s.poll_interval_s})
            self._last_state, self._force_write, self._last_write = state, False, time.monotonic()
            event(log, logging.DEBUG, "cloud.state_written", state=state["status"]["state"])
        await self._sync_challenge(snap.get("challenge"))
        await self._sync_usage()

    async def _sync_challenge(self, ch: dict[str, Any] | None) -> None:
        cid = ch["id"] if ch else None
        if cid == self._published_challenge:
            return
        if ch:
            doc: dict[str, Any] = {"id": cid, "kind": ch.get("kind"), "since": ch.get("since"),
                                   "timeout_s": self.s.challenge_timeout_s}
            img = self.s.state_dir / "challenge" / "captcha.png"
            if ch.get("has_image") and img.exists() and img.stat().st_size <= MAX_IMAGE_BYTES:
                doc["image_png_b64"] = base64.b64encode(img.read_bytes()).decode("ascii")
            await self.db.put("challenge", doc)
            event(log, logging.INFO, "cloud.challenge_published", id=cid, image="image_png_b64" in doc)
        else:
            await self.db.delete("challenge")
            if self._published_challenge is not _UNKNOWN:
                event(log, logging.INFO, "cloud.challenge_cleared")
        self._published_challenge = cid

    async def sync_push(self) -> None:
        try:
            await self._pushes()
        except CloudError as e:
            self._push_fails += 1
            delay = backoff(self._push_fails, self.cfg.retry_min_s, self.cfg.retry_max_s)
            event(log, logging.WARNING, "cloud.push_failed", error=short(str(e)), retry_in_s=delay,
                  streak=self._push_fails)
            self._wake_in(delay)
        else:
            self._push_fails = 0

    async def _pushes(self) -> None:
        snap = self.hub.snapshot()
        ch = snap.get("challenge")
        if ch and ch["id"] not in self._announced:
            await self._push_all({"type": "challenge", "id": ch["id"], "kind": ch.get("kind")}, high=True,
                                 collapse="challenge", ttl_s=self.s.challenge_timeout_s)
            self._announced.add(ch["id"])
        state = (snap.get("status") or {}).get("state")
        if state != self._pushed_state:
            if state in ATTENTION or self._pushed_state in ATTENTION:  # trouble, or the all-clear after it
                err = cloud_state(snap)["status"].get("error")
                await self._push_all({"type": "status", "state": state, "error": err}, high=state in URGENT,
                                     collapse="status", ttl_s=6 * 3600)
            self._pushed_state = state
        q = snap.get("quota")
        if q and q.get("remaining_mb") != self._pushed_mb:
            wait = self.cfg.fcm_quota_min_gap_s - (time.monotonic() - self._pushed_at)
            if wait > 0:
                self._wake_in(wait)
                return
            await self._push_all({"type": "quota", "remaining_mb": q.get("remaining_mb"), "total_mb": q.get("total_mb"),
                                  "fetched_at": q.get("fetched_at"), "state": state},
                                 high=False, collapse="quota", ttl_s=6 * 3600)
            self._pushed_mb, self._pushed_at = q.get("remaining_mb"), time.monotonic()

    async def _get_devices(self) -> dict[str, dict[str, str]]:
        """Paired phones -> FCM target. The app registers its Firebase installation ID (`fid`);
        a legacy registration token (`fcm`) is still used if that's all a phone has."""
        if self._devices is None or time.monotonic() - self._devices_at > self.cfg.devices_cache_s:
            allowed = await self.db.get("allowed") or {}
            devs = await self.db.get("devices") or {}
            self._devices = {}
            for uid, d in devs.items():
                if uid not in allowed or not isinstance(d, dict):
                    continue
                if isinstance(d.get("fid"), str) and d["fid"]:
                    self._devices[uid] = {"fid": d["fid"]}
                elif isinstance(d.get("fcm"), str) and d["fcm"]:
                    self._devices[uid] = {"token": d["fcm"]}
            for target in self._devices.values():
                REDACT.add(next(iter(target.values())), "push_id", keep_last=6)
            self._devices_at = time.monotonic()
        return self._devices

    async def send_test_push(self) -> int:
        """`cloud test-push`: a notification to every paired phone. Returns how many got it."""
        return await self._push_all({"type": "test", "at": iso()}, high=False, collapse="test", ttl_s=600)

    async def _push_all(self, data: dict[str, Any], *, high: bool, collapse: str, ttl_s: int) -> int:
        """Returns the number of phones the message went to."""
        devices = await self._get_devices()
        if not devices:
            event(log, logging.DEBUG, "cloud.push_skipped", type=data["type"], reason="no paired phone with a push id")
            return 0
        payload = {k: str(v) for k, v in data.items() if v is not None}
        sent = 0
        for uid, target in list(devices.items()):
            if await self.fcm.send(target, payload, high=high, collapse_key=collapse, ttl_s=ttl_s) == "unregistered":
                await self.db.delete(f"devices/{uid}")
                devices.pop(uid, None)
                event(log, logging.WARNING, "cloud.device_token_dropped", device=uid[:6])
            else:
                sent += 1
        event(log, logging.INFO, "cloud.push_sent", type=data["type"], high=high, devices=sent)
        return sent

    async def _heartbeat_loop(self) -> None:
        """A write every heartbeat_s even when nothing changed, so the phone can tell
        "nothing new" from "laptop asleep or offline"."""
        while True:
            idle = time.monotonic() - self._last_write
            await asyncio.sleep(max(self.cfg.heartbeat_s - idle, self.cfg.heartbeat_s / 10))
            if time.monotonic() - self._last_write >= self.cfg.heartbeat_s - 1:
                self._force_write = True
                self._dirty.set()

    async def _sync_usage(self) -> None:
        """Mirror the usage history to /usage (app/history.py). The first sync of a process writes
        everything once if Firebase has never had it, else the last few days: it's small."""
        h = self.hub.history
        if h is None:
            return
        if not self._usage_queued:
            marker = self.s.state_dir / "usage_cloud_synced"
            h.queue_all(since=None if not marker.exists() else utcnow() - timedelta(days=3))
            self._usage_queued = True
        pending = h.take_pending()
        if not pending:
            return
        items = list(pending.items())
        try:
            for i in range(0, len(items), 500):  # one multi-path update per 500 paths
                await self.db.patch("usage", dict(items[i:i + 500]))
        except CloudError:
            h.restore_pending(pending)  # rewriting is harmless: same values
            raise
        (self.s.state_dir / "usage_cloud_synced").touch()
        event(log, logging.DEBUG, "cloud.usage_written", paths=len(pending))

    async def _handle_sessions(self, uid: str, events: Any) -> None:
        """WE mobile data on/off from the phone (/inbox/<uid>/session/<id> = {on, t, at})."""
        for key, ev in (list(events.items()) if isinstance(events, dict) else ()):  # a snapshot: we delete as we go
            if ev is None or not UID_RE.match(str(key)):  # our own delete echoing back
                continue
            ok = isinstance(ev, dict) and isinstance(ev.get("on"), bool) and isinstance(ev.get("t"), (int, float))
            result = "malformed" if not ok else "no history"
            if ok and self.hub.history is not None:
                try:
                    result = self.hub.history.add_session_event(ev["on"], int(ev["t"]), uid) or "ignored"
                except Exception:
                    event(log, logging.ERROR, "history.write_failed", exc_info=True)
                    result = "error"
            event(log, logging.INFO if ok else logging.WARNING, "cloud.session_event", device=uid[:6],
                  on=ev.get("on") if ok else None, at=iso(datetime.fromtimestamp(ev["t"] / 1000, timezone.utc)) if ok else None,
                  result=result)
            await self.db.delete(f"inbox/{uid}/session/{key}")
        self._dirty.set()  # upload the session's usage

    # ------------------------------------------------------------------ phone -> hub
    async def _inbox_loop(self) -> None:
        fails = 0
        while True:
            reason, connected = "closed by server", False
            try:
                async with contextlib.aclosing(self.db.stream("inbox")) as events:
                    async for ev, data in events:
                        if not connected:  # the server always opens with a "put" of the current inbox
                            connected = True
                            event(log, logging.INFO, "cloud.inbox_connected", after_failures=fails)
                            fails = 0
                        if ev in ("put", "patch") and isinstance(data, dict):
                            for uid, kind, value in inbox_items(ev, data):
                                await self.handle_inbox(uid, kind, value)
                        elif ev == "auth_revoked":  # token expired: reconnect with a fresh one
                            self.auth.invalidate()
                            reason = "auth_revoked"
                            break
                        elif ev == "cancel":
                            reason = f"cancelled: {short(str(data))}"
                            break
            except CloudError as e:
                fails += 1
                reason = short(str(e))
            delay = backoff(fails, self.cfg.retry_min_s, self.cfg.retry_max_s) if fails else self.cfg.retry_min_s
            event(log, logging.WARNING if fails else logging.INFO, "cloud.inbox_disconnected", reason=reason,
                  retry_in_s=delay, streak=fails)
            await asyncio.sleep(delay)

    async def handle_inbox(self, uid: str, kind: str, value: Any) -> None:
        if value is None:  # a deletion (often our own) echoing back
            return
        if not UID_RE.match(uid):
            event(log, logging.WARNING, "cloud.inbox_bad_uid")
            return
        self._devices = None  # any phone activity: re-read devices on the next push
        dev = uid[:6]
        if kind == "session":  # history, not a command: never too old to record
            await self._handle_sessions(uid, value)
            return
        ok = isinstance(value, dict) and isinstance(value.get("at"), (int, float))
        age_s = (time.time() * 1000 - value["at"]) / 1000 if ok else None
        if not ok or age_s > self.cfg.inbox_max_age_s:
            event(log, logging.WARNING, "cloud.inbox_ignored", device=dev, kind=kind,
                  reason="malformed" if not ok else f"stale ({round(age_s)} s old)")
        elif kind == "answer":
            code, cid = value.get("code"), value.get("id")
            if isinstance(code, str) and CODE_RE.match(code) and isinstance(cid, str):
                result = self.hub.submit_code(code, cid)
                event(log, logging.INFO, "cloud.answer_received", device=dev, result=result)
            else:
                event(log, logging.WARNING, "cloud.inbox_ignored", device=dev, kind=kind, reason="bad code/id")
        elif kind == "refresh":
            wait = self.hub.request_refresh()
            event(log, logging.INFO, "cloud.refresh_requested", device=dev, accepted=wait == 0, retry_in_s=wait)
        elif kind == "hello":
            event(log, logging.INFO, "cloud.phone_hello", device=dev)
        else:
            event(log, logging.WARNING, "cloud.inbox_ignored", device=dev, kind=kind, reason="unknown kind")
        await self.db.delete(f"inbox/{uid}/{kind}")


if __name__ == "__main__":  # `python -m app.cloud check|...` stays the documented command
    from .cloud_cli import main
    main()
