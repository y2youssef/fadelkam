# Failure modes & diagnostics: WE quota scraper

_Last updated: 2026-11-18. Items marked **seen** were observed live; the rest are anticipated._

## Where to look when something breaks

| What | Path (under `server/state/`) | Contents |
|---|---|---|
| Event log | `logs/scraper.jsonl` | One JSON per event: `login.success`, `login.refused`, `guard.hard_stop`, `incident.saved`, `portal.frontend_changed`, ... |
| API log | `logs/api.jsonl` | Every portal API call: endpoint, HTTP status, `retCode`, `errorNo`, `errorMsg`, ms |
| Incidents | `incidents/<UTC time>_<kind>/` | `meta.json` (error, traceback, guard state, portal build), `screenshot.png`, `page.html`, `api_calls.json` (with response bodies) |
| Login ledger | `login_ledger.json` | Every login attempt + outcome; every session's lifetime; lock/backoff/hard-stop state |
| Portal build | `portal_version.json` | Current WE frontend build id and the previous one |
| Captcha | `challenge/captcha.png` | Last captcha image shown by WE |

Quick commands (from `server/`): `python -m app.probe status`, `... page`, `... session`, `... reset`.

All text artefacts pass through the redactor (password, phone number in all its formats, tokens).
**Verified:** React copies the typed password into the page HTML; the incident writer removes it.
Screenshots can't be redacted and show the phone number, so treat `state/` as private.

## Register

| # | Failure | Detection | Automatic response | Human action |
|---|---|---|---|---|
| 1 | **seen twice:** Login refused with *"maximum number of incorrect attempts… try again after one hour"* (`errorNo 60301025072100002`), even though the password was correct. It's a risk/rate response, not a password problem. (a) 11:27 UTC, a bot-looking first attempt (HeadlessChrome UA, `webdriver=true`, brand-new device); the owner could still log in normally minutes later. (b) 13:18 UTC, a login **45 s after a successful one**. Logins 3.5 min+ apart were all accepted. | `errorNo` / message → `AccountLocked` | No login until WE's lock time + 5 min, then **hard stop** (configurable). The guard now enforces **10 min minimum between logins** (`min_login_gap_s`). | Check that you can log in manually, read the incident, then `probe reset`. |
| 2 | Wrong or changed password | non-zero `retCode` → `LoginRejected` | Counted; **hard stop after 2 in a row**. Clears automatically when the credentials change. | Fix creds. |
| 3 | **seen:** WE image captcha ("Verification – Please enter the code below", served from `captcha.te.eg`) | Dialog text, or a dialog containing an image and an input | Captcha image saved and a human asked for the code (terminal/file now; phone notification later). Up to 2 tries per attempt. Unanswered → 30 min backoff. 3 unanswered in a row → hard stop. | Type the code. |
| 4 | SMS one-time code | `getSmsVerificationCode` call or an OTP input field | `ChallengeRequired(sms)` → backoff (entering SMS codes isn't wired yet) | Not handled yet. |
| 5 | Google reCAPTCHA (enabled in portal settings, not seen on login yet) | Visible reCAPTCHA challenge iframe | `ChallengeRequired(recaptcha)` | Needs a headed browser. |
| 6 | **Short sessions** (owner's experience) | Redirect to `#/login`, or an API error mentioning session/token → `SessionExpired` | Re-login through the guard (max **4/hour, 30/day**). Every session's lifetime goes into the ledger. | If sessions turn out shorter than ~15 min, the caps will bite; revisit after measuring. |
| 7 | **Single active session** (unconfirmed): logging in on phone/web kicks the scraper out, and vice versa | Sessions that end right after the owner used the app | Same as #6 | Test: log in on the phone while the scraper runs, then check `session`. |
| 8 | Request signing: every call carries `*ReqBodySign`/`*ReqHeaderSign` from a Rust→WASM white-box module | — | **Design choice:** all calls run inside the real page, so the app signs them. No re-implementation to break. | — |
| 9 | WE deploys a new frontend (selectors/routes/flows change) | `portal.frontend_changed` warning; selector timeouts → `PortalChanged` + incident | No retry loop on `PortalChanged`. All selectors are in one place (`SEL` in `we_client.py`). | Re-run `probe page` / `probe login`, update selectors. |
| 10 | F5/WAF block page (more likely from **datacenter/VPS IPs**) | Block-page text → `WafBlocked` | Backoff | Prefer a residential or Egyptian IP; test the VPS early. |
| 11 | Headless fingerprinting beyond UA/webdriver | More captchas or refusals than in headed mode | Already mitigated: clean UA, `webdriver=false`, Africa/Cairo timezone, stable profile | Fallbacks: headed Chrome under Xvfb in Docker, or Camoufox (Scrapling). |
| 12 | `deviceid` changes (profile wiped or volume missing) → WE sees a new device → more challenges | `portal.deviceid_changed` warning | — | Keep `state/` on a persistent Docker volume. **Verified:** with the profile kept, deviceid stays the same across restarts. |
| 13 | `queryFreeUnit` shape unknown (multiple buckets, DSL vs mobile, units) | Parser can't find the bucket → loud error; raw response kept | — | First live probe captures it. |
| 14 | Number is `010…` (not WE-native `015`) | `creds.warning` at startup | — | Confirm the line is on WE (ported) and that the portal shows mobile data for it. |
| 15 | Browser crash / profile lock left behind / memory creep | Launch error mentioning `Singleton*` | Stale lock removed, relaunch once | Poller will restart the browser periodically (TODO). |
| 16 | Network outage / timeouts / 5xx | `TransientError` | Exponential backoff 1→30 min | — |
| 17 | Clock skew on the server (the app sends `x-client-time`) | Unexplained refusals after deploy | — | Make sure NTP is on the VPS. |
| 18 | Corrupt ledger file | JSON parse error | **Fail closed** (hard stop) | Inspect and reset. |
| 19 | Disk fill | — | Logs rotate (5 MB × 10 per file); incidents capped at 50 | — |
| 20 | **seen:** The portal's anti-DevTools script (`disabledDebug.js` = `disable-devtool` v0.3.9) fires under automation. Its Performance detector (type 6) triggers in about 10 s, and the app then calls `logOutForUser`, which killed a fresh session after 2 s. | `portal.antidebug_triggered`, a `portal.dialog` "Prohibit use of the console!", `portal.app_logged_out` | Script replaced by an empty stub at the network layer (`portal.antidebug_stubbed` logged once per browser). Verified: 0 triggers. | If tripwires fire, WE renamed or moved the script; update `ANTIDEBUG_SCRIPT`. |
| 21 | **seen:** The session is the `indiv_login_token` cookie, which is httpOnly and **non-persistent**, plus `sessionStorage` (`curLoginId`). | `browser.started session_restored=`, `session.check cookie= storage=` | Chrome launched with `--restore-last-session`, which keeps the session cookie across restarts. **Verified:** a restart resumed the session with no login, and the first reading came 34 s after start. The profile preference can't be used: Chrome reverts it (signature-protected on Windows). "Logged in" is decided by the cookie; sessionStorage is empty in a new tab. If `#/login` sends us to a member page, we're logged in (`login.already_logged_in`). | If `session_restored=false` after restarts, each restart costs a login. |
| 26 | **seen:** After a captcha submit, the closing dialog was mistaken for a second captcha even though login had succeeded | `login.captcha_shown round=2` right after `captcha_submitted` | Wait for the auth response or the dialog to close; after a successful auth, captcha dialogs are ignored | — |
| 24 | **seen:** A restored or extra tab runs its own copy of the SPA. An idle one called `logOutForUser` exactly **15:00 after it loaded**, ending the shared session. | `browser.extra_tabs_closed`, `portal.app_logged_out` | Only one tab is kept at startup | — |
| 25 | **seen twice, answered:** The SPA logs itself out (`logOutForUser`, which also ends the session server-side) **15:00 after a document load** (12:12:57→12:27:57, 13:01:47→13:16:48). Route changes and API calls every 2 min don't reset it, and the session outlived "15 min after login". This is why WE sessions feel short. | `portal.app_logged_out` at doc-age ≈ 900 s | **Keepalive:** once the document is older than 11 min, the poll is a real reload (`session.keepalive_reload`), which restarts their timer and keeps the cookie. **Verified live:** the reload fired at doc age 699 s; the session kept its cookie and lived past the old load+15 min mark with no logout. | If `app_logged_out` still appears, check its doc age in the log. |
| 27 | **seen:** Restarting the browser ~20 s after a login lost the session: Chrome writes cookies to disk with a delay (~30 s). The forced re-login then hit #1(b). | `browser.started session_restored=false` right after a login | Min login gap (#1) stops this from cascading | Don't restart within ~1 min of a login. |
| 29 | Graceful shutdown (Ctrl-C, `docker stop`) loses the session: `--restore-last-session` was only proven after hard kills | `session_restored=false` after a clean stop | The session cookie is saved to `state/session_cookies.json` after every login and poll and at shutdown, and restored at startup if Chrome didn't (`session.cookies_restored_from_disk`). Deleted when WE ends the session. | — |
| 30 | **Laptop host:** sleep stops polling; after a long sleep WE has probably ended the session | Phone shows `stale`; `session.ended` after wake | Guarded re-login on wake (captcha possible, answered from the phone) | Keep it awake on AC power for 24/7 data |
| 31 | **Laptop host:** moving between networks changes the public IP. Unknown whether WE binds the session to the IP. | `session.ended` right after a network change | Guarded re-login | Collect evidence before doing anything about it |
| 32 | Two machines (this PC and the laptop) logging into the account close together → "max attempts" (#1b). Each guard only sees its own machine. | `login_account_locked` incident | — | Stop one machine at least 10 min before the other logs in |
| 33 | API exposed through a tunnel: token leak or brute force | `api.auth_failed` with client IP | Bearer token, 32 random bytes, constant-time compare; binds to 127.0.0.1 only | Rotate: delete `state/api_token`, restart, re-enter it in the app |
| 28 | Server-side session lifetime: **still unknown**. Every session so far was ended by the client timer (#25) or a restart. | `session.ended` with no `app_logged_out` before it | Guarded re-login | Read lifetimes from `probe status` once the keepalive has run for a few hours. |
| 22 | **seen:** `/overview` calls `queryFreeUnit` only once per page load (and not at all right after login), so later visits return nothing | `quota.route_no_call` | Primary route is now `/accountoverview`, which re-fetches every visit (~10 calls, <1 s). Same route → hop via `/usage`. Miss → one full reload (`quota.reload_used`). After login, wait for networkidle + 3 s. | If `quota.reload_used` shows up often, the hop stopped working. |
| 23 | Captcha frequency unknown (0 captchas in 3 logins after the fingerprint fix) | `login.captcha_shown` + ledger | Human in the loop | If frequent: local OCR with a confidence gate, max 1 machine guess per login (wrong answers count toward lockout), then fall back to a human. |
