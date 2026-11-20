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

### As a systemd service (Linux; how the laptop ran it until 2026-11-20)

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

## Docker (another always-on machine)

`server/Dockerfile` + `compose.yaml` (repo root): Python 3.14 and **Google Chrome** (the same browser
as the laptop, so the browser profile and WE's view of the device carry over), running as uid 1000,
`state/` in the volume `we-state`, `creds.txt` as a Docker secret, `restart: unless-stopped`,
graceful `docker compose stop` (closes Chrome, saves the session, tells the phone "stopped"), and a
health check on `/healthz`. Tests run in the image, without the state or the creds: `docker run --rm we-quota python -m pytest -q`.

**Only one machine may run the server** (CLAUDE.md). The instance lock works within one machine
only, so moving it is a manual hand-over:

1. **New machine:** install Docker, clone the repo, put `creds.txt` in the repo root
   (`phone: …` / `password: …`, private).
2. **Laptop:** stop it for good and pack its state (a live session token and the Firebase key:
   move the file privately and delete it afterwards):
   ```bash
   systemctl --user disable --now we-quota          # also writes "stopped" to the phone
   tar czf ~/we-state.tgz -C ~/fadelkam/server state
   ```
3. **New machine**, from the repo root, with `we-state.tgz` next to `compose.yaml`:
   ```bash
   docker compose build
   docker compose run --rm --no-deps -v "$PWD/we-state.tgz:/tmp/we-state.tgz:ro" --entrypoint sh \
       we-quota -c "tar xzf /tmp/we-state.tgz -C /app/server/state --strip-components=1"
   docker compose up -d
   docker compose logs -f we-quota      # want: browser.started session_restored=True, quota.reading
   ```
   Same profile, so WE sees the same device; if the session is still alive (likely within an
   hour or so) there is no login at all. If one is needed, the copied login ledger still enforces
   the gaps, and a captcha goes to the phone as usual. The phone needs no change.
4. **Keep the machine awake and Docker starting at boot.** The phone's "checked HH:MM" shows when
   it isn't.

**On Windows (Docker Desktop, WSL 2 backend)**, PowerShell in the repo folder:
```powershell
docker compose build
docker compose run --rm --no-deps -v "${PWD}\we-state.tgz:/tmp/we-state.tgz:ro" --entrypoint sh `
    we-quota -c "tar xzf /tmp/we-state.tgz -C /app/server/state --strip-components=1"
docker compose up -d
docker compose logs -f we-quota
Remove-Item we-state.tgz          # it holds a live session token and the Firebase key
```
`creds.txt` can be made in Notepad (two lines, `phone: …` and `password: …`; Windows line endings
are fine). Docker Desktop only runs while you're signed in to Windows, so for 24/7:
- Docker Desktop → Settings → General: **Start Docker Desktop when you sign in**.
- Lock the screen instead of signing out. Power: **Sleep: Never** (plugged in), no hibernate.
- Windows Update restarts: Settings → Accounts → Sign-in options → **use my sign-in info to
  finish setting up after an update**, so Windows signs back in and Docker starts again.
- Clock: keep "Set time automatically" on (Firebase sign-in and captcha answers depend on it).

**Day to day:** edit the code, `docker compose build`, run the tests in the new image
(`docker run --rm we-quota python -m pytest -q`), and only then `docker compose up -d`: it replaces
the running server (graceful stop, then a start on the same volume; keep it more than 1 min away
from a login). Diagnostics run inside the live container: `docker compose exec we-quota python -m
app.probe status` (also `app.cloud check`, `app.usage days 7`). The image keeps the Chrome it was
built with; to update Chrome, `docker compose build --no-cache`, then `docker compose up -d`.

Building: the image installs the current Chrome stable (it must be at least as new as the profile).
On a network without IPv6, apt is forced to IPv4 and retries; a build can still fail on a flaky
Debian mirror: just run it again.

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
