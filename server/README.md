# WE quota server

Logs in to my.te.eg with a real browser, reads remaining mobile data every 90–180 s, and
serves it to the phone over HTTP + Server-Sent Events. Read `../docs/FAILURE_MODES.md` before
changing login behaviour: WE locks out logins that come too fast.

## Setup (Linux, e.g. CachyOS)

```bash
cd server
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
playwright install chromium      # bundled Chromium (not officially supported on Arch,
                                 # usually works); or install google-chrome and pass --chrome
printf 'phone: 01XXXXXXXXX\npassword: ...\n' > ../creds.txt   # git-ignored
pytest -q                        # offline tests, no network
```

## Run

```bash
python -m app.server [--chrome] [--headed]   # API on 127.0.0.1:8080 + poller
```

- The first start writes the API token to `state/api_token`. Copy it into the phone app.
- If WE shows a captcha: answer it with `POST /challenge` (the phone does this), or
  `echo CODE > state/challenge/code.txt`. The image is at `state/challenge/captcha.png`.
- Don't run two instances, and don't run this machine and another one against the same
  account at once. Logins less than ~1 min apart get refused as "max attempts". The guard
  enforces 10 min between logins on *this* machine only.

## API (all but /healthz need `Authorization: Bearer <token>`)

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

Evidence lives in `state/` (private, git-ignored): `logs/scraper.jsonl` (events),
`logs/api.jsonl` (every portal call), `incidents/<time>_<kind>/` (screenshot, HTML, API
calls, error), `login_ledger.json`, `last_quota.json`, `session_cookies.json` (a live session
token, chmod 600).

## Settings (env)

`WE_POLL_INTERVAL_S` (180), `WE_API_HOST` (127.0.0.1), `WE_API_PORT` (8080), `WE_API_TOKEN`,
`WE_BROWSER_CHANNEL` (`chrome` for the installed Chrome), `WE_HEADLESS` (1), `WE_STATE_DIR`,
`WE_PHONE` / `WE_PASSWORD` (instead of creds.txt), `WE_MIN_LOGIN_GAP_S` (600),
`WE_MAX_LOGINS_PER_HOUR` (4). The rest are in `app/config.py`.
