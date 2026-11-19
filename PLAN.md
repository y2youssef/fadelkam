# WE Mobile-Data Quota Tracker — Project Plan

**Goal:** Track remaining **mobile internet data** (not DSL/landline) on the WE Egypt
self-service portal (`https://my.te.eg/echannel/`), poll it every ~3 minutes, and show
the remaining amount in a **persistent ("sticky") notification on an Android phone** that
updates in place.

_Last updated: 2026-11-19_

---

## 1. Locked decisions

| Topic | Decision |
|---|---|
| **Where it runs** | The owner's **Linux laptop (CachyOS)** on home Wi-Fi does the scraping. Changed 2026-11-19: WE may flag non-Egyptian or datacenter IPs, and the session lives 16 h+ on a residential IP |
| **Packaging** | Plain venv on the laptop. Docker image later, optional (e.g. a home Pi) |
| **Phone side** | **Thin custom Android APK** (not ntfy app / MacroDroid) — gives a true in-place sticky notification |
| **Push transport** | **Firebase** (changed 2026-11-19, replacing SSE + tunnel). The laptop writes to Realtime Database and sends FCM; the phone reads live while awake and is woken by FCM when asleep. Only outbound HTTPS from the laptop: no tunnel, no router, no domain. Compared against Tailscale (needs an always-on phone VPN) and Cloudflare Worker + D1 (can't wake a sleeping phone without adding FCM anyway). The local SSE API stays for development |
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
┌──────────────────── Laptop (CachyOS, home Wi-Fi, Egyptian IP) ────────────────────┐
│  Playwright/Chrome (persistent profile) ──guarded login──▶ my.te.eg               │
│        ▼                                                                          │
│  Poller (90–180 s; keepalive reload before the SPA's 15 min logout)               │
│        ▼                                                                          │
│  Hub (latest reading, status, pending captcha) ──▶ local API + SSE (development)  │
│        ▼                                                                          │
│  CloudPublisher ──HTTPS──▶ Realtime Database  /state  /challenge                  │
│        │       ──HTTPS──▶ FCM (captcha = high priority, trouble, reading changed) │
│        ◀──stream── /inbox/<phone>/{answer, refresh, hello}                        │
└───────────────────────────────────────────────────────────────────────────────────┘
                              │ Firebase (free Spark plan)
                              ▼
┌──────────────── Samsung phone (Android 15 / One UI 7) ─────────────────┐
│  Thin APK:                                                             │
│   • Pairing: anonymous sign-in, shows a 6-digit code, laptop approves  │
│   • ONE ongoing/sticky notification, updated in place                  │
│   • Live /state listener while the screen is on; FCM wakes it when not │
│   • Captcha: notification with the image → reply → /inbox/<uid>/answer│
│   • "Updated N min ago" / "laptop offline" from the heartbeat          │
└────────────────────────────────────────────────────────────────────────┘
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

### 4.2b Server: cloud relay (`server/app/cloud.py`)
- [x] Service-account JWT → OAuth token (RTDB + FCM scopes), no Google SDK (httpx + cryptography,
  which work on Py 3.15).
- [x] Whitelisted `/state` written on change + 5 min heartbeat; `/challenge` with the captcha image;
  `stopped` written on a clean shutdown.
- [x] FCM HTTP v1: captcha (high), needs-human/login-failed (high), broken/error and the all-clear
  (normal), reading changed (normal, at most every 2 min). Dead tokens dropped.
- [x] `/inbox` stream: captcha answers (≤15 min old), refresh requests, hello.
- [x] Security rules (`firebase/database.rules.json`): deny by default, phone scoped to its own uid
  once paired. CLI: `check`, `deploy-rules`, `pair`, `devices`, `revoke`, `test-push`.
- [x] 20 offline tests against a fake Firebase (signature check, whitelist, retries, captcha round trip).
- [ ] Owner creates the Firebase project (server/README.md, "Cloud"); first live `cloud check`.

### 4.3 Packaging (`server/`)
- [ ] `Dockerfile` based on `mcr.microsoft.com/playwright/python:vX.Y` (pinned, Chromium preinstalled).
- [ ] `docker-compose.yml` (env for creds, volume for `state/`, port map).
- [ ] `.env.example`, `requirements.txt` (pinned).

### 4.4 Android app (`android/`, package `com.fadelkam.wequota`)
- [ ] Kotlin project, minSdk 26, targetSdk 35 (Android 15). Firebase BoM: Auth, Realtime Database, Messaging.
- [ ] Pairing screen: anonymous sign-in → write `/pairing/<uid>` with a 6-digit code → wait for `/allowed/<uid>`.
- [ ] Register the FCM token at `/devices/<uid>` (and on `onNewToken`); write `/inbox/<uid>/hello`.
- [ ] Single ongoing notification, updated in place: remaining / total, "updated N min ago",
  greyed out when the heartbeat is older than ~2.5 × `heartbeat_s`, "server stopped" on `stopped`.
- [ ] `/state` listener only while the screen is on (saves data); FCM data messages update the
  notification when it's off.
- [ ] Captcha: high-priority notification with the image (from `/challenge`) and a reply action →
  `/inbox/<uid>/answer` with `ServerValue.TIMESTAMP`.
- [ ] Refresh action → `/inbox/<uid>/refresh`.
- [ ] Runtime `POST_NOTIFICATIONS` permission (Android 13+); battery-optimization exemption prompt (One UI 7).

---

## 5. Open risks & unknowns
- **SMS/captcha frequency.** If WE challenges on *every* headless login, full automation needs
  frequent human input. Mitigation: keep the session alive as long as possible; re-login rarely;
  human-in-the-loop fallback via `/challenge`.
- **`queryFreeUnit` response shape** for a personal mobile line is not yet confirmed — needs one
  live login to capture the exact JSON and locate the right "free unit" bucket (data vs voice vs SMS;
  and separating mobile-data from any DSL bundle on the same login).
- **Token lifetime** unknown — determines re-login frequency. The owner reports sessions are short;
  the login ledger now records every session's lifetime. **Answered:** one session lasted 20 h+ with the
  keepalive, and a login in the My WE app doesn't end it (2026-11-19, FAILURE_MODES #7).
- **Service number starts with 010** (not WE-native 015) — must be a line ported to WE; confirm the
  portal shows its mobile data bundle.
- **Server reachability from phone:** solved by the Firebase relay (2026-11-19). The laptop only
  makes outbound calls, and the phone never connects to the laptop.
- **F5/WAF may flag datacenter IPs** or a repeated pattern; 3-min cadence should be fine but monitor.

---

## 6. Environment status

### Laptop (CachyOS), the production host since 2026-11-19
- Repo at `~/fadelkam`. Python **3.14.10**, venv at `server/.venv`, all pinned deps install, 77/77
  offline tests pass (`server/pytest.ini` puts `server/` on the import path so plain `pytest -q` works).
- Google Chrome 158 at `/opt/google/chrome/chrome` → run with `--chrome` (bundled Chromium not installed).
- `probe page` 2026-11-19 14:53 UTC: login form found, no WAF block, UA `X11; Linux x86_64`,
  `webdriver=false`, portal build `main.b8367340062733f882fa.chunk.js`. New profile = new `deviceid`.
- Clock: NTP active and synced, timezone Africa/Cairo.
- `creds.txt` chmod 600, `server/state/` chmod 700. The old PC's `state/` was not carried over (its
  Chrome profile is Windows-encrypted and useless here). Old PC stopped and the project deleted
  there on 2026-11-19, a few minutes before 14:57 UTC.
- Runs as the systemd user service `we-quota` (`server/systemd/we-quota.service`), enabled, with
  linger, so it starts at boot before login. The Wi-Fi password is stored system-wide, so the network
  is up before login too.
- First laptop login 2026-11-19 15:18:46 UTC, no captcha; first reading 15:18:51 (3631 of 6250 MB). Firebase still off (no key yet).
- Awake: Plasma never suspends when idle and ignores the lid (AC and battery). Optional root step for
  the login screen: `server/systemd/logind-lid-on-ac.conf`.

### Old PC (Windows), retired
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
1. ~~De-risk login~~: done 2026-11-18.
2. ~~Scraper + poller hardened~~: done; one session has lasted 16 h+.
3. ~~Local API + SSE + captcha relay~~: done (now for development).
4. ~~Cloud relay (Firebase)~~: code done 2026-11-19; waiting for the owner's Firebase project.
5. ~~Move to the laptop~~ (CachyOS): venv, creds, systemd service: done 2026-11-19. Firebase key pending.
6. **Android toolchain** on the laptop (JDK 17, Android SDK platform 35, Gradle).
7. **Thin APK** (4.4): pairing, FCM, sticky notification, captcha reply.
8. **End-to-end test** on the Samsung phone; tune One UI 7 battery settings.
9. Later, optional: Docker image; OCR captcha with a confidence gate if captchas turn out frequent.

---

## 9. Immediate next steps
- [ ] Owner: create the Firebase project (5 console steps in `server/README.md`, "Cloud"), put the
      key in `server/state/`, run `python -m app.cloud deploy-rules` and `check`.
- [x] Stop the server on the old PC **at least 10 min** before the laptop's first login (done 2026-11-19).
- [ ] Owner, optional: install `server/systemd/logind-lid-on-ac.conf` (root) so the lid doesn't
      suspend at the login screen while plugged in.
- [ ] Android toolchain on the laptop, then the APK.
