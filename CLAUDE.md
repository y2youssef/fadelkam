# WE quota tracker: notes for Claude

Scrapes remaining mobile data from my.te.eg (WE Egypt) on the owner's laptop and relays it through
Firebase to a sticky notification on their Samsung phone. Read `PLAN.md` (decisions, architecture,
status, next steps) and `docs/FAILURE_MODES.md` (what breaks, what was seen live, where the
evidence is) before changing behaviour.

## Account safety: WE locks the account out
- Log in to WE only through the guarded code (`app.server`, `app.poller`, `app.probe login`), and only
  when the owner is around to answer a captcha. Never script a login around the guard.
- Never let two instances or two machines use the account at the same time. Before the first login on
  another machine, stop the old one and wait at least 10 min. A login 45 s after another one was
  refused as "max attempts" and locked the account for an hour.
- Don't restart the server within ~1 min of a login: Chrome writes the session cookie to disk late.
- A guard hard stop means a human checks why first (`python -m app.probe status`, the incident
  folder), and only then runs `probe reset`, with the service stopped (it refuses otherwise: the running
  server keeps its own copy of the ledger in memory).
- On the laptop the server runs as the systemd user service `we-quota` (starts at boot). Don't run
  `python -m app.server` or a browser probe next to it; they refuse anyway (`state/instance.lock`).
  Stop the service first, and keep restarts more than 1 min away from a login.

## Secrets
- `creds.txt` (repo root) and `server/state/` are private and git-ignored. `server/state/` holds the
  browser profile, session cookies, API token, Firebase service-account key, logs and incidents.
  Never print, log or commit their contents. Before committing, scan the staged changes for the
  password, the phone number (all formats) and tokens.
- Everything sent to Firebase goes through the whitelist in `server/app/cloud.py` (`cloud_state`).

## How the owner wants the work done
- Anticipate failure modes, log enough to diagnose afterwards (JSON lines in `server/state/logs/`),
  and record anything newly seen live in `docs/FAILURE_MODES.md`, with times and evidence.
- Prove safety claims with tests (`cd server && pytest -q`, offline, no network).

## Commands (from `server/`)
- `systemctl --user start|stop|restart|status we-quota`, `journalctl --user -u we-quota -f`: the
  running server (unit file in `server/systemd/`)
- `python -m app.server [--chrome]`: poller + local API + Firebase uploader
- `python -m app.probe status|session|page|reset`: guard state and diagnostics
- `python -m app.cloud check|deploy-rules|pair|devices|revoke|test-push`: Firebase relay
