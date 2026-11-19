"""Real-browser client for my.te.eg.

Why a browser for *every* call (not just login): each API request carries `*ReqBodySign` /
`*ReqHeaderSign` headers produced by a Rust->WebAssembly white-box signing module inside the
portal's JS (`sign_white` before login, `sign_login` after). Re-implementing that outside the
browser would break on every portal release. So the app signs its own requests, and we
navigate the SPA and capture the JSON responses.

A persistent browser profile (state/profile) keeps cookies, localStorage and the portal's
`deviceid` stable across restarts: WE sees one consistent device, which is our best lever
against repeated SMS/captcha challenges.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .config import Credentials, Settings
from .diagnostics import ApiRecorder, save_incident
from .errors import (AccountLocked, ApiError, ChallengeRequired, PortalChanged, SessionExpired, TransientError,
                     WafBlocked, WeError, classify_login_failure)
from .logs import REDACT, event, get_logger
from .login_guard import LoginGuard

log = get_logger("client")

# Everything we depend on in the portal's UI lives here, so a portal change is a one-line fix.
SEL = {
    "login_id": "#login_loginid_input_01",
    "password": "#login_password_input_01",
    "submit": "#login-withecare",
}
LOGIN_ENDPOINT = "/auth/userAuthenticate"
SMS_ENDPOINTS = ("getSmsVerificationCode", "validateVerifyCode")
WAF_MARKERS = ("The requested URL was rejected", "Request Rejected", "Your support ID is")
CAPTCHA_TEXT = "Please enter the code below"
ANTIDEBUG_SCRIPT = "**/disabledDebug.js*"
# Storage keys that indicate a logged-in SPA. `curLoginId` (sessionStorage) seen after a real
# login; `indiv_login_token` appears in the bundle's logout code. The real check is still
# "does a protected route stay off #/login" (fetch_via_route raises SessionExpired).
TOKEN_KEYS = ("curLoginId", "indiv_login_token")
SESSION_COOKIE = "indiv_login_token"  # httpOnly, non-persistent (dies with the browser)

# (challenge_kind, captcha_image_path) -> code typed by a human, or None to give up
CodeProvider = Callable[[str, "Path | None"], Awaitable["str | None"]]


class WeClient:
    def __init__(self, settings: Settings, creds: Credentials, guard: LoginGuard) -> None:
        self.s = settings
        self.creds = creds
        self.guard = guard
        self.recorder = ApiRecorder(settings)
        self._pw = None
        self.ctx = None
        self.page: Page | None = None
        self._auth_hits: list[Response] = []
        self._auth_sent = 0
        self.antidebug_stubbed = 0
        self.antidebug_hits = 0
        self._doc_loaded_at: float | None = None
        self._saved_cookies: str | None = None
        self._quota_miss_streak = 0
        REDACT.add(creds.password, "password")
        REDACT.add(creds.phone, "phone", keep_last=3)

    # ------------------------------------------------------------------ lifecycle
    async def _single_page(self) -> Page:
        """Keep exactly one tab. Restored tabs run their own copy of the SPA, and an idle one
        logged our shared session out after 15 min (12:27:57 on 2026-11-18)."""
        await asyncio.sleep(2)  # session restore opens tabs asynchronously
        pages = self.ctx.pages
        keep = pages[0] if pages else await self.ctx.new_page()
        for p in pages[1:]:
            await p.close()
        if len(pages) > 1:
            event(log, logging.INFO, "browser.extra_tabs_closed", count=len(pages) - 1)
        return keep

    async def start(self) -> None:
        self.s.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        ua = await self._clean_user_agent()
        kwargs: dict[str, Any] = dict(
            channel=self.s.browser_channel, headless=self.s.headless, locale="en-US", timezone_id="Africa/Cairo",
            viewport={"width": 1366, "height": 900}, user_agent=ua,
            # Don't advertise automation (navigator.webdriver, HeadlessChrome UA). Measured: both are
            # visible to the portal by default.
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled",
                  # Restore the previous session, which keeps session-only cookies (WE's
                  # `indiv_login_token`). A process restart / deploy / crash then doesn't cost a
                  # login while WE still accepts the session (seen live after a crash-restore).
                  # The profile preference does the same but is signature-protected on Windows,
                  # so Chrome reverts outside edits; the switch overrides it.
                  "--restore-last-session"],
        )
        try:
            self.ctx = await self._pw.chromium.launch_persistent_context(str(self.s.profile_dir), **kwargs)
        except PlaywrightError as e:
            if "Singleton" not in str(e) and "already in use" not in str(e):
                raise
            # A crashed previous run can leave Chrome's profile lock behind.
            event(log, logging.WARNING, "browser.stale_profile_lock", error=str(e)[:200])
            for p in self.s.profile_dir.glob("Singleton*"):
                p.unlink(missing_ok=True)
            self.ctx = await self._pw.chromium.launch_persistent_context(str(self.s.profile_dir), **kwargs)
        self.ctx.set_default_timeout(self.s.nav_timeout_ms)
        self.ctx.set_default_navigation_timeout(self.s.nav_timeout_ms)
        self.recorder.attach(self.ctx)
        self.ctx.on("response", self._on_any_response)
        self.ctx.on("request", self._on_any_request)
        # The portal ships `disable-devtool` (disabledDebug.js). Under automation its Performance
        # detector (type 6) fires within seconds; the app then redirects to #/login and calls
        # logOutForUser -> we were logged out 2 s after a successful login (2026-11-18).
        # Serve an empty script instead, and keep tripwires in case WE renames/moves it.
        await self.ctx.route(ANTIDEBUG_SCRIPT, self._stub_antidebug)
        self.ctx.on("page", self._attach_tripwires)
        for p in self.ctx.pages:
            self._attach_tripwires(p)
        self.page = await self._single_page()
        self.page.on("load", lambda _p: self._on_load())  # real document loads only, not hash routes
        self._on_load()  # a restored tab loaded moments ago; never leave the age unknown
        await self._restore_session_cookies()
        restored = await self.has_session_token()
        if restored:
            self.guard.resume_session()
        else:
            # The session from the previous process is gone. That's not a WE expiry, so keep it
            # out of the lifetime stats.
            self.guard.end_session("process_restart", expired=False)
        event(log, logging.INFO, "browser.started", channel=self.s.browser_channel or "bundled-chromium",
              session_restored=restored, headless=self.s.headless, user_agent=ua, profile=str(self.s.profile_dir))

    async def _clean_user_agent(self) -> str | None:
        if not self.s.headless:
            return None
        b = await self._pw.chromium.launch(channel=self.s.browser_channel, headless=True)
        try:
            ua = await (await b.new_page()).evaluate("navigator.userAgent")
        finally:
            await b.close()
        return ua.replace("HeadlessChrome", "Chrome")

    # ------------------------------------------------------------------ session cookie persistence
    @property
    def _cookie_file(self) -> Path:
        return self.s.state_dir / "session_cookies.json"

    async def save_session_cookies(self) -> None:
        """Backstop for --restore-last-session (proven only after hard kills, and it lost a
        20-s-old cookie). The file holds a live session token: state/ is private, and the file
        is chmod 600 where supported."""
        if not self.ctx:
            return
        try:
            cookies = [c for c in await self.ctx.cookies() if "te.eg" in c["domain"]]
        except PlaywrightError:
            return
        if not any(c["name"] == SESSION_COOKIE and c["value"] for c in cookies):
            return
        for c in cookies:
            if c["name"] == SESSION_COOKIE:
                REDACT.add(c["value"], "session")
        text = json.dumps(cookies)
        if text == self._saved_cookies:
            return
        tmp = self._cookie_file.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self._cookie_file)
        self._saved_cookies = text

    async def _restore_session_cookies(self) -> bool:
        if not self._cookie_file.exists():
            return False
        if any(c["name"] == SESSION_COOKIE and c["value"] for c in await self.ctx.cookies()):
            return False  # Chrome already restored the session
        try:
            cookies = json.loads(self._cookie_file.read_text(encoding="utf-8"))
            await self.ctx.add_cookies(cookies)
        except (OSError, ValueError, PlaywrightError) as e:
            event(log, logging.WARNING, "session.cookie_restore_failed", error=str(e)[:200])
            return False
        event(log, logging.INFO, "session.cookies_restored_from_disk", count=len(cookies))
        return True

    def forget_session_cookies(self) -> None:
        self._cookie_file.unlink(missing_ok=True)
        self._saved_cookies = None

    async def close(self) -> None:
        await self.save_session_cookies()
        for closer in (getattr(self.ctx, "close", None), getattr(self._pw, "stop", None)):
            if closer:
                try:
                    await closer()
                except Exception:
                    pass
        event(log, logging.INFO, "browser.closed")

    async def __aenter__(self) -> "WeClient":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ------------------------------------------------------------------ state probes
    async def storage_snapshot(self) -> dict[str, list[str]]:
        """Key names only (never values) of local/session storage on the portal origin."""
        try:
            return await self.page.evaluate(
                "() => ({local: Object.keys(localStorage), session: Object.keys(sessionStorage)})")
        except PlaywrightError:
            return {"local": [], "session": []}

    async def has_session_token(self) -> bool:
        """True if the browser holds WE's session cookie (httpOnly `indiv_login_token`)."""
        # No URL filter: filtering by base_url missed the cookie after a session restore (2026-11-18).
        cookie = any(c["name"] == SESSION_COOKIE and c["value"] and "te.eg" in c["domain"]
                     for c in await self.ctx.cookies())
        storage = False
        if cookie:
            if not self.page.url.startswith(self.s.base_url):
                try:  # e.g. sitting on chrome-error:// after an outage
                    await self.page.goto(self.s.base_url, wait_until="domcontentloaded")
                except PlaywrightError as e:  # 2026-11-19 00:50: DNS down escaped as "unexpected"
                    raise TransientError(f"navigation failed: {str(e)[:200]}") from e
            for _ in range(3):  # a restored tab may still be navigating -> "execution context destroyed"
                try:
                    storage = await self.page.evaluate(
                        "keys => keys.some(k => !!(localStorage.getItem(k) || sessionStorage.getItem(k)))",
                        list(TOKEN_KEYS))
                    break
                except PlaywrightError:
                    try:
                        await self.page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    except PlaywrightError:
                        pass
        event(log, logging.INFO, "session.check", cookie=cookie, storage=storage, url=self.page.url)
        # The cookie decides. sessionStorage is per tab: a new tab is empty until the SPA
        # rebuilds it from the cookie (seen 2026-11-18: cookie valid, storage empty, still logged in).
        # If the server has expired the cookie, fetch_via_route sees the redirect to #/login.
        return cookie

    async def _register_tokens(self) -> None:
        try:
            vals = await self.page.evaluate("""() => [...Object.entries(localStorage), ...Object.entries(sessionStorage)]
                .filter(([k, v]) => /token|auth|session/i.test(k) && typeof v === 'string' && v.length >= 8)
                .map(([k, v]) => v)""")
            for v in vals:
                REDACT.add(v, "token")
        except PlaywrightError:
            pass

    async def _check_waf(self) -> None:
        try:
            html = await self.page.content()
        except PlaywrightError:
            return
        if any(m in html for m in WAF_MARKERS):
            raise WafBlocked("F5/WAF block page served instead of the portal")

    def _on_login_page(self) -> bool:
        return "#/login" in self.page.url

    # ------------------------------------------------------------------ login
    async def login(self, code_provider: CodeProvider | None = None) -> None:
        """One guarded login. Raises a WeError subclass on any failure (after saving an incident).

        `code_provider(challenge, image_path)` is awaited when WE shows its image captcha; it
        returns the code a human read off the image, or None to give up."""
        self.guard.check()
        page = self.page
        t0 = time.monotonic()

        # --- phase 1: reach the form (nothing submitted yet -> not an attempt)
        try:
            await page.goto(self.s.login_url, wait_until="domcontentloaded")
            await self._check_waf()
            await page.wait_for_selector(SEL["login_id"], state="visible")
        except WafBlocked as e:
            e.incident = await save_incident(self.s, "waf_blocked", page=page, recorder=self.recorder, error=e)
            raise
        except (PlaywrightTimeout, PlaywrightError) as e:
            # The SPA sends a logged-in browser from #/login to a member page (seen: #/accountoverview).
            # That alone proves the session is alive, whatever the storage heuristics say.
            if not self._on_login_page() and self.page.url.startswith(self.s.base_url):
                event(log, logging.INFO, "login.already_logged_in", url=self.page.url,
                      token_check=await self.has_session_token())
                return
            err = PortalChanged(f"login form not reachable: {type(e).__name__}: {str(e)[:200]}")
            err.incident = await save_incident(self.s, "login_form_missing", page=page, recorder=self.recorder, error=err)
            raise err from e

        # --- phase 2: fill and *verify* the form before spending an attempt
        await page.wait_for_timeout(random.randint(600, 1400))
        for sel, value in ((SEL["login_id"], self.creds.phone), (SEL["password"], self.creds.password)):
            loc = page.locator(sel)
            await loc.click()
            await loc.fill("")
            await loc.press_sequentially(value, delay=random.randint(40, 110))
        if (await page.locator(SEL["login_id"]).input_value() != self.creds.phone
                or await page.locator(SEL["password"]).input_value() != self.creds.password):
            err = PortalChanged("login form did not accept the credentials as typed (input mask/format changed?)")
            err.incident = await save_incident(self.s, "login_form_mismatch", page=page, recorder=self.recorder, error=err)
            raise err
        await page.wait_for_timeout(random.randint(300, 900))

        # --- phase 3: submit (this is the attempt the guard counts)
        attempt = self.guard.begin_attempt()
        err: WeError | None
        self._auth_hits.clear()
        self._auth_sent = 0
        try:
            await page.click(SEL["submit"])
            err = await self._resolve_login(code_provider)
        except PlaywrightError as e:
            err = TransientError(f"browser error during login: {str(e)[:200]}")
        except BaseException as e:
            # A bug or Ctrl-C mid-login: still close the attempt record and leave evidence.
            outcome = "unknown" if self._auth_sent else "aborted"
            self.guard.end_attempt(attempt, outcome, WeError(f"{type(e).__name__}: {e}"))
            await save_incident(self.s, "login_crashed", page=page, recorder=self.recorder, error=e,
                                extra={"attempt": attempt, "credentials_sent": bool(self._auth_sent)})
            raise
        if err is not None and not self._auth_sent and LoginGuard.outcome_for(err) == "unknown":
            err = TransientError(f"login not sent: {err}")

        if err is None:
            self.guard.end_attempt(attempt, "success")
            await self._register_tokens()
            event(log, logging.INFO, "login.success", seconds=round(time.monotonic() - t0, 1), url=page.url)
            # Let the landing page finish loading the subscriber context (querySubscribers etc.).
            # Navigating away immediately left /overview without its usage widget (2026-11-18).
            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
            except PlaywrightError:
                pass
            await page.wait_for_timeout(3000)
            await self.save_session_cookies()
            return
        self.guard.end_attempt(attempt, LoginGuard.outcome_for(err), err)
        err.incident = await save_incident(self.s, f"login_{err.kind}", page=page, recorder=self.recorder, error=err,
                                           extra={"attempt": attempt, "guard": self.guard.status()})
        event(log, logging.ERROR, "login.failed", **err.as_dict())
        raise err

    async def _post_login_checks(self) -> WeError | None:
        """retCode 0 can still be followed by an SMS step; wait for the SPA to leave the login page."""
        # Auth already succeeded, so a captcha dialog still on screen is stale. Look only for later steps (SMS).
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not self._on_login_page():
                return None
            ch = await self._detect_challenge(include_image_captcha=False)
            if ch:
                return ch
            await asyncio.sleep(0.5)
        return await self._detect_challenge(include_image_captcha=False) or PortalChanged(
            "login returned retCode 0 but the app stayed on the login page (new post-login step?)")

    async def _stub_antidebug(self, route) -> None:
        self.antidebug_stubbed += 1
        if self.antidebug_stubbed == 1:
            event(log, logging.INFO, "portal.antidebug_stubbed", url=route.request.url)
        await route.fulfill(status=200, content_type="application/javascript", body="/* stubbed */")

    def _attach_tripwires(self, page: Page) -> None:
        def on_dialog(dialog) -> None:
            self.antidebug_hits += "console" in dialog.message.lower()
            event(log, logging.WARNING, "portal.dialog", message=dialog.message[:200], type=dialog.type,
                  antidebug_hits=self.antidebug_hits)
            asyncio.ensure_future(dialog.dismiss())

        def on_console(msg) -> None:
            if "DEVTOOL" in msg.text:
                event(log, logging.ERROR, "portal.antidebug_triggered", text=msg.text[:200],
                      hint="anti-devtool fired despite the stub; it will log the session out")

        page.on("dialog", on_dialog)
        page.on("console", on_console)

    def _on_any_response(self, resp: Response) -> None:
        if LOGIN_ENDPOINT in resp.url:
            self._auth_hits.append(resp)

    def _on_any_request(self, req) -> None:
        if LOGIN_ENDPOINT in req.url:
            self._auth_sent += 1  # credentials actually left the browser

    async def _wait_login_signal(self) -> tuple[str, Any]:
        """Race: the userAuthenticate response vs. a challenge dialog vs. timeout."""
        deadline = time.monotonic() + self.s.api_timeout_ms / 1000
        while time.monotonic() < deadline:
            if self._auth_hits:
                return "response", self._auth_hits.pop()
            ch = await self._detect_challenge()
            if ch:
                return "challenge", ch
            await asyncio.sleep(0.3)
        return "timeout", None

    async def _resolve_login(self, code_provider: CodeProvider | None) -> WeError | None:
        rounds = 0
        while True:
            kind, val = await self._wait_login_signal()
            if kind == "timeout":
                try:
                    await self._check_waf()
                except WafBlocked as w:
                    return w
                return TransientError("no login response and no challenge within timeout")

            if kind == "response":
                resp: Response = val
                try:
                    body = await resp.json()
                except Exception:
                    body = None
                header = (body or {}).get("header") or {}
                if resp.status >= 500 or body is None:
                    return TransientError(f"login HTTP {resp.status}, body not JSON")
                if str(header.get("retCode")) == "0":
                    return await self._post_login_checks()
                failure = classify_login_failure(header)
                event(log, logging.WARNING, "login.refused", **failure.as_dict())
                # The app may answer a refusal by opening its captcha dialog; give it a moment.
                await asyncio.sleep(1.5)
                ch = await self._detect_challenge()
                if not (ch and ch.challenge == "image_captcha" and not isinstance(failure, AccountLocked)):
                    return failure
                val = ch

            # ---- a challenge is on screen
            ch: ChallengeRequired = val
            if ch.challenge != "image_captcha" or code_provider is None:
                return ch
            if rounds >= self.s.max_captcha_rounds:
                event(log, logging.WARNING, "login.captcha_rounds_exhausted", rounds=rounds)
                await self._captcha_cancel()
                return ch
            rounds += 1
            img = await self._save_captcha_image()
            event(log, logging.WARNING, "login.captcha_shown", round=rounds, image=str(img))
            try:
                code = await asyncio.wait_for(code_provider(ch.challenge, img), timeout=self.s.challenge_timeout_s)
            except asyncio.TimeoutError:
                code = None
            if not code:
                event(log, logging.WARNING, "login.captcha_unanswered", round=rounds)
                await self._captcha_cancel()
                return ch
            self._auth_hits.clear()
            await self._captcha_submit(code.strip())
            event(log, logging.INFO, "login.captcha_submitted", round=rounds, length=len(code.strip()))
            # The dialog stays visible for a moment after Ok. Checking right away mistook it for a new
            # captcha even though login had succeeded (2026-11-18). Wait for the response or the close.
            await self._await_after_captcha()
            # loop: next signal is the auth response, or a fresh captcha if the code was wrong

    async def _await_after_captcha(self) -> None:
        deadline = time.monotonic() + self.s.api_timeout_ms / 1000
        while time.monotonic() < deadline:
            if self._auth_hits:
                return
            try:
                if not await self._captcha_dialog().first.is_visible():
                    return
            except PlaywrightError:
                return
            await asyncio.sleep(0.3)

    def _captcha_dialog(self):
        return self.page.get_by_role("dialog").filter(has=self.page.locator("img")).filter(
            has=self.page.locator("input"))

    async def _save_captcha_image(self) -> Path | None:
        d = self.s.state_dir / "challenge"
        d.mkdir(parents=True, exist_ok=True)
        path = d / "captcha.png"
        dialog = self._captcha_dialog().first
        try:
            img = dialog.locator("img").first
            await (img if await img.count() else dialog).screenshot(path=str(path))
        except PlaywrightError as e:
            event(log, logging.ERROR, "login.captcha_image_failed", error=str(e)[:200])
            return None
        return path

    async def _captcha_submit(self, code: str) -> None:
        dialog = self._captcha_dialog().first
        inp = dialog.locator("input").first
        await inp.fill("")
        await inp.press_sequentially(code, delay=random.randint(60, 140))
        await dialog.get_by_role("button", name=re.compile(r"^\s*(ok|confirm|submit|تأكيد|موافق)\s*$", re.I)).click()

    async def _captcha_cancel(self) -> None:
        try:
            await self._captcha_dialog().first.get_by_role(
                "button", name=re.compile(r"^\s*(cancel|إلغاء)\s*$", re.I)).click(timeout=5000)
        except PlaywrightError:
            pass

    async def _detect_challenge(self, include_image_captcha: bool = True) -> ChallengeRequired | None:
        if any(self.recorder.last(e) for e in SMS_ENDPOINTS):
            return ChallengeRequired("portal requested an SMS verification code", challenge="sms")
        try:
            # WE's own image captcha (seen 2026-11-18): a dialog titled "Verification",
            # "Please enter the code below", an <img>, a refresh button, an input, Cancel/Ok.
            if include_image_captcha and (await self.page.get_by_text(CAPTCHA_TEXT).first.is_visible() or
                                          await self._captcha_dialog().first.is_visible()):
                return ChallengeRequired("WE image captcha shown on login", challenge="image_captcha")
        except PlaywrightError:
            pass
        checks = (
            ("recaptcha", "iframe[src*='recaptcha'][src*='bframe']:visible"),
            ("slider_captcha", "[class*='slider' i][class*='verify' i]:visible"),
            ("sms", "input[placeholder*='verification' i]:visible, input[placeholder*='OTP' i]:visible"),
        )
        for kind, sel in checks:
            try:
                if await self.page.locator(sel).count():
                    return ChallengeRequired(f"{kind} challenge shown on login", challenge=kind)
            except PlaywrightError:
                continue
        return None

    async def ensure_logged_in(self, code_provider: CodeProvider | None = None) -> None:
        if await self.has_session_token() and not self._on_login_page():
            return
        await self.login(code_provider)

    # ------------------------------------------------------------------ data calls
    def _on_load(self) -> None:
        self._doc_loaded_at = time.monotonic()

    def doc_age_s(self) -> float:
        return time.monotonic() - self._doc_loaded_at if self._doc_loaded_at else 0.0

    def portal_doc_live(self) -> bool:
        """A logged-in portal document is open, so its 15 min logout timer is running."""
        url = self.page.url if self.page else ""
        return url.startswith(self.s.base_url) and not self._on_login_page()

    async def fetch_quota_raw(self) -> dict[str, Any]:
        """queryFreeUnit, with misses on a slow network reported as transient.

        A miss is PortalChanged (needs a code fix, long backoff) only after quota_miss_escalate
        misses in a row; before that it's TransientError, so the poller retries in about a minute."""
        try:
            body = await self._fetch_quota_once()
        except PortalChanged as e:
            self._quota_miss_streak += 1
            n, limit = self._quota_miss_streak, self.s.quota_miss_escalate
            if n >= limit:
                raise
            event(log, logging.WARNING, "quota.miss_as_transient", streak=n, escalate_at=limit, error=str(e))
            raise TransientError(f"{e} [miss {n}/{limit}: slow network or portal change]",
                                 incident=e.incident) from e
        self._quota_miss_streak = 0
        return body

    async def _fetch_quota_once(self) -> dict[str, Any]:
        """queryFreeUnit via a route visit; if the app doesn't call it, one full reload.

        Keepalive: the SPA runs a client-side timer that calls logOutForUser 15 min after the
        *document* loaded. Route changes and API calls don't reset it (seen twice on 2026-11-18,
        both at load+15:00). A real reload starts a new document and restarts the timer, and the
        session cookie survives it. So once the document is older than doc_max_age_s, the poll is
        a reload."""
        age = self.doc_age_s()
        if age >= self.s.doc_max_age_s:
            event(log, logging.INFO, "session.keepalive_reload", doc_age_s=round(age))
            return await self.fetch_via_route(self.s.quota_route, "queryFreeUnit", force_reload=True)
        try:
            return await self.fetch_via_route(self.s.quota_route, "queryFreeUnit", save_incident_on_miss=False,
                                              timeout_ms=self.s.route_wait_ms)
        except PortalChanged:
            event(log, logging.WARNING, "quota.route_no_call", route=self.s.quota_route, trying="reload")
        body = await self.fetch_via_route(self.s.quota_route, "queryFreeUnit", force_reload=True)
        event(log, logging.WARNING, "quota.reload_used", route=self.s.quota_route)
        return body

    async def fetch_via_route(self, route: str, endpoint: str, save_incident_on_miss: bool = True,
                              timeout_ms: int | None = None, force_reload: bool = False) -> dict[str, Any]:
        """Open an SPA route and return the JSON of the `endpoint` call the app makes there."""
        page = self.page
        url = self.s.route_url(route)
        # A reload has to load the whole document before the app can call anything.
        timeout_ms = timeout_ms or (max(self.s.api_timeout_ms, self.s.nav_timeout_ms) if force_reload
                                    else self.s.api_timeout_ms)
        try:
            if page.url == url and not force_reload:  # same route = no re-mount = no call; hop away first
                await page.goto(self.s.route_url(self.s.refresh_hop_route), wait_until="domcontentloaded")
                await page.wait_for_timeout(random.randint(800, 1600))
            if force_reload and page.url != url:
                await page.goto(url, wait_until="domcontentloaded")  # hash change only; reload below
            async with page.expect_response(lambda r: endpoint in r.url, timeout=timeout_ms) as info:
                if force_reload:
                    await page.reload(wait_until="domcontentloaded")  # new document
                else:
                    await page.goto(url, wait_until="domcontentloaded")
            resp = await info.value
        except PlaywrightTimeout as e:
            await asyncio.sleep(1)
            if self._on_login_page():
                self.guard.end_session("redirected_to_login")
                self.forget_session_cookies()
                raise SessionExpired(f"redirected to login while opening {route}") from e
            await self._check_waf()
            err = PortalChanged(f"route {route} did not call {endpoint} within {timeout_ms} ms"
                                f"{' (after reload)' if force_reload else ''}")
            if save_incident_on_miss:
                err.incident = await save_incident(self.s, "route_no_call", page=page, recorder=self.recorder,
                                                   error=err, extra={"route": route, "endpoint": endpoint})
            raise err from e
        except PlaywrightError as e:
            raise TransientError(f"navigation failed: {str(e)[:200]}") from e

        if resp.status >= 500:
            raise TransientError(f"{endpoint} HTTP {resp.status}")
        try:
            body = await resp.json()
        except Exception as e:
            raise TransientError(f"{endpoint} returned non-JSON (HTTP {resp.status})") from e
        header = body.get("header") or {}
        if str(header.get("retCode")) != "0":
            msg = str(header.get("errorMsg") or "")
            kw = {"error_no": header.get("errorNo"), "ret_code": header.get("retCode")}
            if self._on_login_page() or any(w in msg.lower() for w in ("session", "login", "token", "expired")):
                self.guard.end_session(f"api:{header.get('errorNo')}")
                self.forget_session_cookies()
                raise SessionExpired(msg or "session expired", **kw)
            err = ApiError(msg or f"{endpoint} retCode {header.get('retCode')}", **kw)
            err.incident = await save_incident(self.s, "api_error", page=page, recorder=self.recorder, error=err)
            raise err
        return body
