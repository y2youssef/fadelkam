# WE quota server

Logs in to my.te.eg with a real browser, reads remaining mobile data every 90–180 s, and
pushes it to Firebase, where the phone reads it (live while awake, woken by FCM push when
asleep). A local HTTP + SSE API is there for development. Read `../docs/FAILURE_MODES.md`
before changing login behaviour: WE locks out logins that come too fast.

## Setup (Linux, e.g. CachyOS)

```bash
cd server
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
playwright install chromium      # bundled Chromium (not officially supported on Arch,
                                 # usually works); or install google-chrome and pass --chrome
$EDITOR ../creds.txt && chmod 600 ../creds.txt   # two lines: "phone: 01…" and "password: …"
                                 # (an editor, not echo/printf: those leave the password in shell history)
pytest -q                        # offline tests, no network
```

## Run

```bash
python -m app.server [--chrome] [--headed]   # API on 127.0.0.1:8080 + poller
```

- If WE shows a captcha, the phone gets a push notification with the image and sends the code
  back through Firebase. Without the phone: `echo CODE > state/challenge/code.txt`. The image
  is at `state/challenge/captcha.png`.
- The first start writes the local API token to `state/api_token` (development only).
- Don't run two instances, and don't run this machine and another one against the same
  account at once. Logins less than ~1 min apart get refused as "max attempts". The guard
  enforces 10 min between logins on *this* machine only. On one machine a second server,
  poller or browser probe refuses to start (lock on `state/instance.lock`).

### As a service (the laptop)

```bash
systemctl --user enable ~/fadelkam/server/systemd/we-quota.service   # start with the user manager
loginctl enable-linger "$USER"                                     # ...which starts at boot
systemctl --user start|stop|restart|status we-quota
journalctl --user -u we-quota -f                                   # console log
```

The unit sets `WE_BROWSER_CHANNEL=chrome`, waits up to 60 s for the network, restarts after a
crash only after 2 min (Chrome writes the session cookie late, FAILURE_MODES #27), and makes
everything it writes owner-only.

Keeping the laptop awake: in Plasma, *System Settings → Power Management* has "When inactive:
Do nothing" and "When laptop lid closed: Do nothing", for AC and battery. Before anyone logs
in (after a reboot) logind handles the lid itself and suspends. To keep it awake on AC there
too: `sudo install -Dm644 systemd/logind-lid-on-ac.conf /etc/systemd/logind.conf.d/we-quota.conf`
(takes effect at the next boot).

## Cloud (Firebase): how the phone gets the data

The laptop only makes outbound HTTPS calls to Firebase, and the phone reads from there. Nothing
listens for incoming connections, so it works on any Wi-Fi with no router or tunnel setup, and
WE only ever sees the laptop's home IP. The free (Spark) plan is plenty.

**One-time setup** (~15 min at https://console.firebase.google.com):
1. **Create a project.** Google Analytics: off.
2. **Build → Realtime Database → Create database**, location **Belgium (europe-west1)**, start in
   **locked mode**.
3. **Build → Authentication → Get started → Sign-in method → Anonymous → Enable.**
4. **Project settings → Service accounts → Generate new private key.** Save it as
   `server/state/firebase-service-account.json` and `chmod 600` it. It is admin access to the
   Firebase project: never commit it, never copy it to the phone.
5. **Project settings → General → Add app → Android**, package `com.fadelkam.y2y` (the one registered for this project).
   Download `google-services.json` into `android/app/` (git-ignored).

Then, from `server/`:
```bash
python -m app.cloud deploy-rules   # upload ../firebase/database.rules.json
python -m app.cloud check          # token, DB round trip, rules deployed?, paired phones
python -m app.server               # the uploader starts by itself when the key file exists
```
**Test the captcha path without WE:** `python -m app.cloud simulate-captcha` publishes a picture of a
random code as a captcha, pushes the phone, and checks the answer it sends back.

**Pairing:** in the app tap Pair. It shows a 6-digit code. Run `python -m app.cloud pair` and
confirm the same code. Also: `devices`, `revoke <uid>`, `test-push`.

What's in the database: `/state` (reading, poller status, heartbeat every 5 min), `/challenge`
(only while a captcha is pending, with its image), `/allowed` and `/devices` (paired phones
and their push tokens), `/pairing`, and `/inbox` (phone → laptop: captcha answers, refresh
requests). The phone number, password, cookies, raw portal responses and local paths never
leave the laptop. The security rules (`../firebase/database.rules.json`) deny everything by
default. A paired phone can read `/state` and `/challenge` and write only its own `/inbox` and
`/devices` entries.

If the database isn't in europe-west1, set `WE_FIREBASE_DB_URL` (shown on the Realtime Database
page). `WE_CLOUD=0` turns the uploader off; `WE_CLOUD=1` makes a missing key an error.

## Local API, for development (all but /healthz need `Authorization: Bearer <token>`)

| Method | Path | |
|---|---|---|
| GET | `/healthz` | liveness, poller state, no personal data |
| GET | `/quota` | snapshot: quota, buckets, `stale`, poller status, pending challenge |
| GET | `/events` | SSE: `snapshot` on connect, then `quota` / `status` / `challenge`; `: ping` every 20 s |
| GET | `/challenge`, `/challenge/image` | pending captcha + its PNG |
| POST | `/challenge` | `{"code": "...", "id": "<challenge id>"}` |
| POST | `/refresh` | poll now (max once per 30 s) |

## Operate

```bash
python -m app.probe status    # guard state, recent logins, session lifetimes
python -m app.probe reset     # clear a hard stop after a human checked why it happened
python -m app.probe page      # load the login page only: WAF / selectors / build id
```

`page`, `login` and `session` open the browser, so stop the service first. `reset` refuses while
the service runs (the server keeps its own copy of the ledger in memory): stop, reset, start.

Evidence lives in `state/` (private, git-ignored): `logs/scraper.jsonl` (events),
`logs/api.jsonl` (every portal call), `incidents/<time>_<kind>/` (screenshot, HTML, API
calls, error), `login_ledger.json`, `last_quota.json`, `session_cookies.json` (a live session
token, chmod 600).

## Settings (env)

`WE_POLL_INTERVAL_S` (180), `WE_API_HOST` (127.0.0.1), `WE_API_PORT` (8080), `WE_API_TOKEN`,
`WE_BROWSER_CHANNEL` (`chrome` for the installed Chrome), `WE_HEADLESS` (1), `WE_STATE_DIR`,
`WE_PHONE` / `WE_PASSWORD` (instead of creds.txt), `WE_MIN_LOGIN_GAP_S` (600),
`WE_MAX_LOGINS_PER_HOUR` (4), `WE_CLOUD` (auto), `WE_FIREBASE_SA`, `WE_FIREBASE_DB_URL`,
`WE_CLOUD_HEARTBEAT_S` (300). The rest are in `app/config.py` and `app/cloud.py`.
