# WE Mobile-Data Quota Tracker — Project Plan

**Goal:** Track remaining **mobile internet data** (not DSL/landline) on the WE Egypt
self-service portal (`https://my.te.eg/echannel/`), poll it every ~3 minutes, and show
the remaining amount in a **persistent ("sticky") notification on an Android phone** that
updates in place.

_Last updated: 2026-11-18_

---

## 1. Locked decisions

| Topic | Decision |
|---|---|
| **Where it runs** | Always-on **server/VPS** does the scraping |
| **Packaging** | **Portable Docker** image (runs on any Linux VPS / Raspberry Pi / NAS) |
| **Phone side** | **Thin custom Android APK** (not ntfy app / MacroDroid) — gives a true in-place sticky notification |
| **Push transport** | Server → phone over **SSE** (Server-Sent Events). No Firebase/FCM dependency |
| **Login automation** | **Fully automatic headless** login via real browser (Playwright). WE "sometimes asks SMS/captcha" → build a human-in-the-loop fallback |
| **Robustness** | **Robust from the start**: token refresh, retries/backoff, logging, graceful captcha/SMS handling |
| **APK delivery** | Set up the Android toolchain **on this PC** and build a ready-to-sideload debug APK |
| **Target phone** | **Samsung, Android 15 (One UI 7)** — aggressive background-kill; needs foreground service + battery-optimization exemption |

---

## 2. Portal reconnaissance (findings)

`my.te.eg/echannel` is a React SPA talking to a REST backend. Pulled from the app's JS bundle:

### Endpoints
- **Base:** `https://my.te.eg/echannel/`
- **API gateway wrapper:** `/echannel/service/besapp/base/rest/busiservice`
- **Login:** `cz/v1/auth/userAuthenticate` (also `userAuthenticateForLegacy`, `...ForCorpo`)
  - Password is **RSA-encrypted client-side** before being sent (public key baked into the app).
- **Mobile data quota:** `cz/cbs/bb/queryFreeUnit` — "free units" = the remaining data bundle. **This is the target call.**
- Related/possibly useful: `cz/cbs/bb/queryFamilyUsage`, `queryFamilyShared`,
  `getTransferableFreeUnits`, `cbs/ar/queryBalance`, `cbs/ar/queryTotalBalance`.
- Auth support: `getSmsVerificationCode`, `validateVerifyCode`, `validateToken`, `logOutForUser`.

### Bot / anti-automation defenses (the real challenge)
1. **F5 BIG-IP / ASM cookie** (`TS01...`) set on first response — enterprise bot-mitigation; can issue a JS challenge. **This is why we drive a real browser, not raw `requests`.**
2. **Google reCAPTCHA** — behind a feature flag (`google.recaptcha.enable` / `google.recaptcha.sitekey`); fires conditionally.
3. **Slider captcha** + **SMS verification code** — also conditional.
4. Route cookie: `echannelweb_route`.

**Implication:** a pure HTTP client is fragile/likely to be blocked. A **headless real browser**
(Playwright/Chromium) handles RSA, the F5 JS challenge, and reCAPTCHA/SMS naturally.

### Live recon findings (2026-11-18) — changed the design
- **Every API request is signed.** Headers `whitereqbodysign`/`whitereqheadersign` (pre-login,
  "white" key) and the post-login equivalent are produced by a **Rust→WebAssembly white-box
  module** in the bundle (`sign_white` / `sign_login`, `__wbindgen_*`). Other headers: `csrftoken`,
  `deviceid`, `x-init-time` (from `localStorage.initTime`), `x-client-time`, `channelid: 702`.
  ⇒ **Replaying `queryFreeUnit` from plain HTTP is not viable.** All calls stay inside the
  real page; the poller drives the SPA and captures the JSON responses.
- **WE image captcha** ("Verification – Please enter the code below", `captcha.te.eg`) can pop up
  on login. Handled as human-in-the-loop (image → human → code typed into the dialog).
- First headless login attempt was refused with *"maximum number of incorrect attempts… try
  again after one hour"* (`errorNo 60301025072100002`, which the app groups under
  `CAPTCHA_INVALID_ERROR`). The password was correct, and the owner could still log in normally
  minutes later. Most likely cause: a risk check against the bot-looking default headless
  browser (`HeadlessChrome` UA + `navigator.webdriver=true`, both measured). Fixed with a clean UA,
  no automation flag and a **persistent profile**, which keeps `deviceid` the same across runs (verified).
- Portal params: `google.recaptcha.enable=Y`, `signin.block.minutes=15`, `verify.code.length=6`.
- Candidate SPA routes for usage: `/home`, `/homemobile`, `/overview`, `/usage`, `/accountoverview`.
- Full failure register + where logs go: **`docs/FAILURE_MODES.md`**.

---

## 3. Architecture

```
┌─────────────────────────── VPS (Docker) ───────────────────────────┐
│                                                                     │
│  Playwright/Chrome (persistent profile) ──login──▶ my.te.eg         │
│        │ (guarded: caps, lockout & captcha handling)                │
│        ▼                                                            │
│  Poller (every 3 min) ──▶ open SPA route; the app makes the signed  │
│        │                  queryFreeUnit call; we capture its JSON   │
│        │                                                            │
│        ▼                                                            │
│  State store (latest value) ──▶ FastAPI:                            │
│        • GET /quota        (current value, for app launch/reconnect)│
│        • GET /events       (SSE stream, pushes updates)             │
│        • POST /challenge   (submit SMS/captcha code when prompted)  │
│        • (push alert when a challenge is needed)                    │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ SSE over HTTPS
                               ▼
┌──────────────── Samsung phone (Android 15 / One UI 7) ──────────────┐
│  Thin APK:                                                          │
│   • Foreground service holds SSE connection (auto-reconnect+backoff)│
│   • ONE ongoing/sticky notification, updated in place               │
│   • Settings: server URL + access token                             │
│   • Requests battery-optimization exemption (One UI 7)              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 4. Components to build

### 4.1 Server — scraper/auth (`server/app/`)
- [x] `config.py` — env-driven settings; tolerant creds loader (file or `WE_PHONE`/`WE_PASSWORD`).
- [x] `logs.py` — JSON-lines logs (`scraper.jsonl`, `api.jsonl`, rotated) + secret redactor.
- [x] `errors.py` — failure taxonomy + login-error classifier (known `errorNo`s).
- [x] `login_guard.py` — persistent ledger; caps; lockout/rejection/challenge policies; session lifetimes.
- [x] `diagnostics.py` — API recorder, portal-build + deviceid tracking, incident bundles.
- [x] `we_client.py` — persistent-profile browser, guarded login, image-captcha round trip,
  `fetch_via_route()` (app-signed calls). *Session-token key + post-login flow unconfirmed until first login.*
- [x] `probe.py` — ops/recon CLI (`creds|status|reset|page|login|session`).
- [x] `tests/test_safety.py` — 23 offline tests (guard policies, classifier, creds, redaction).
- [x] First successful live login (2026-11-18, no captcha once the fingerprint was fixed). Session =
  `indiv_login_token` httpOnly **session cookie** + sessionStorage. `queryFreeUnit` comes from
  `/overview` (light) or `/accountoverview` (post-login landing, heavier).
- [x] Anti-devtool script (`disabledDebug.js`) stubbed; it was logging us out 2 s after login.
- [x] `quota.py` — parses `queryFreeUnit` (unit codes from the portal's CBS_MEASURE_UNIT dict);
  `last_quota.json` + `last_quota_raw.json` kept in `state/`. Tests use the real response.
- [x] `poller.py` — loop with per-failure-class delays and status; route fallback; no deliberate
  browser restarts (would cost a login).
- [x] Found why WE sessions are short: the SPA calls `logOutForUser` 15 min after each page load.
  Fixed with a keepalive reload once the document is 11 min old (verified live).
- [x] Session survives poller restarts (`--restore-last-session`; logged-in check uses the cookie).
- [x] Guard: at least 10 min between logins (a login 45 s after another was refused as "max attempts").
- [ ] Measure real server-side session lifetime with the long-running poller (running since 2026-11-18 17:58 UTC).
- [ ] SMS-code challenge (same code-provider path as the captcha).

### 4.2 Server — API + push (`server/app/api.py`)
- [x] Starlette app (FastAPI dropped: pydantic's compiled core is an avoidable risk on Py 3.15, and
  isn't needed for six endpoints): `/healthz`, `/quota`, `/events` (SSE, 20 s heartbeat),
  `/challenge` (+ `/image`, POST code), `/refresh`. Poller runs in the same process (`app/server.py`).
- [x] Bearer-token auth (generated into `state/api_token`); binds to 127.0.0.1; reached via tunnel.
- [x] Session cookie backed up to `state/` → graceful restarts don't cost a login.
- [x] Challenge workflow: when login needs SMS/captcha, mark state `challenge_pending`,
  push an SSE alert ("enter SMS code"); app (or a web form) POSTs the code to `/challenge`.

### 4.3 Packaging (`server/`)
- [ ] `Dockerfile` based on `mcr.microsoft.com/playwright/python:vX.Y` (pinned, Chromium preinstalled).
- [ ] `docker-compose.yml` (env for creds, volume for `state/`, port map).
- [ ] `.env.example`, `requirements.txt` (pinned).

### 4.4 Android app (`android/`)
- [ ] Kotlin project, minSdk 26, targetSdk 35 (Android 15).
- [ ] Foreground service (type `dataSync`/`specialUse`) holding an SSE client (OkHttp-EventSource).
- [ ] Single ongoing notification, updated in place (same notification ID).
- [ ] Settings screen: server URL + token (stored in encrypted prefs).
- [ ] Reconnect with backoff; show "stale/disconnected" state in the notification.
- [ ] Runtime `POST_NOTIFICATIONS` permission (Android 13+); battery-optimization exemption prompt (One UI 7).
- [ ] Optional: tap notification → open portal / app; action button to force-refresh.

---

## 5. Open risks & unknowns
- **SMS/captcha frequency.** If WE challenges on *every* headless login, full automation needs
  frequent human input. Mitigation: keep the session alive as long as possible; re-login rarely;
  human-in-the-loop fallback via `/challenge`.
- **`queryFreeUnit` response shape** for a personal mobile line is not yet confirmed — needs one
  live login to capture the exact JSON and locate the right "free unit" bucket (data vs voice vs SMS;
  and separating mobile-data from any DSL bundle on the same login).
- **Token lifetime** unknown — determines re-login frequency. The owner reports sessions are short;
  the login ledger now records every session's lifetime. Possible single-active-session policy (unconfirmed).
- **Service number starts with 010** (not WE-native 015) — must be a line ported to WE; confirm the
  portal shows its mobile data bundle.
- **Server reachability from phone:** VPS with public IP/domain = simplest. Home Pi needs
  port-forward or a tunnel (Tailscale/Cloudflare Tunnel).
- **F5/WAF may flag datacenter IPs** or a repeated pattern; 3-min cadence should be fine but monitor.

---

## 6. Environment status (this PC)
- Primary dir: `D:\fadelkam` (git initialized).
- Python **3.15.2** locally. **Playwright 1.65.2 installs and runs fine** (greenlet ships cp315
  wheels). venv at `server/.venv`. Local runs use the installed Google Chrome
  (`WE_BROWSER_CHANNEL=chrome` or `--chrome`); Docker will use the bundled Chromium.
- **Docker: not installed** → need Docker Desktop (or test the scraper on the VPS directly).
- **Android: no SDK, no Gradle, only JDK 8** → need **JDK 17 + Android cmdline-tools + platform-35 +
  build-tools + Gradle** to build the APK.
- Phone: **Samsung / Android 15**.

---

## 7. Credentials
- `creds.txt` is in the project (git-ignored). Code reads it **at runtime only** and never logs/prints it.
- Expected format (to be finalized): line 1 = WE service/phone number, line 2 = password
  — or `phone: ...` / `password: ...` key:value lines. Loader will be tolerant of both.
- Will move to a `.env` / Docker secret for deployment.

---

## 8. Build phases (execution order)
1. **De-risk login (highest priority).** One real Playwright login → capture `queryFreeUnit`
   JSON → confirm we can read remaining mobile data. Decides everything downstream.
2. **Scraper + poller** hardened (token reuse, re-login, backoff).
3. **FastAPI + SSE + challenge** endpoint.
4. **Dockerfile + compose**; deploy to VPS; confirm reachable over HTTPS.
5. **Android toolchain setup** on this PC.
6. **Thin APK**: foreground service + SSE + sticky notification + settings.
7. **End-to-end test** on the Samsung phone; tune One UI 7 battery settings.
8. **Hardening/monitoring**: logs, health check, alerting on repeated login failure.

---

## 9. Immediate next steps
- [x] Confirm `creds.txt` format (`phone:` / `password:` lines; loader handles both formats).
- [x] Password confirmed working by the owner (manual login, 2026-11-18).
- [ ] Run `python -m app.probe login --chrome` (owner present for a possible captcha) → captures
      the post-login flow, the token key, and which route calls `queryFreeUnit`.
- [ ] Measure session lifetime (`probe session` at intervals) and test the single-session theory.
- [ ] Decide server dev approach: install Docker Desktop here **or** develop/test directly on the VPS.
- [ ] Confirm VPS details (public domain/IP, OS) for deployment + phone reachability.
