# WE quota tracker: notes for Claude

Scrapes remaining mobile data from my.te.eg (WE Egypt) on the owner's Windows PC (in Docker) and relays
it through Firebase to a sticky notification on their Samsung phone. Read `PLAN.md` (decisions, architecture,
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
- Since 2026-11-20 the server runs in Docker on the owner's Windows PC (`compose.yaml`, service
  `we-quota`, restart unless-stopped). Deploying a code change (`docker compose up -d --build`)
  restarts it: keep that more than 1 min away from a login. Don't run `python -m app.server` or a
  browser probe next to it (on the same state they refuse: `state/instance.lock`), and never on a
  second machine: the lock can't see other machines.
- The CachyOS laptop is retired: its `we-quota` service is disabled and its `server/state/` is
  stale. Never start the server there again.

## Secrets
- `creds.txt` (repo root, git-ignored) and the server state are private. The state lives in the
  Docker volume `we-state` (`/app/server/state` in the container; `server/state/`, git-ignored, when
  run from a venv) and holds the browser profile, session cookies, API token, Firebase
  service-account key, usage history, logs and incidents.
  Never print, log or commit their contents. Before committing, scan the staged changes for the
  password, the phone number (all formats) and tokens.
- Everything sent to Firebase goes through the whitelist in `server/app/cloud.py` (`cloud_state`).

## How the owner wants the work done
- Anticipate failure modes, log enough to diagnose afterwards (JSON lines in `state/logs/`),
  and record anything newly seen live in `docs/FAILURE_MODES.md`, with times and evidence.
- Prove safety claims with tests (offline, no network): `docker run --rm we-quota python -m pytest -q`
  after `docker compose build`, or `cd server && pytest -q` in a venv.

## Commands
Repo root, PowerShell on the PC; the server runs in the container `we-quota`:
- `docker compose logs -f we-quota` (console log), `docker compose stop|start`
- Deploy a code change: `docker compose build`, test the new image
  (`docker run --rm we-quota python -m pytest -q`: no state, no creds, offline), then
  `docker compose up -d` (replaces the running server; not within ~1 min of a login)
- In the running server, `docker compose exec we-quota python -m <module>`:
  - `app.probe status`: guard state (`session`, `page` and `login` need the browser and refuse
    while the server runs). Reset after a hard stop, once a human has checked: `docker compose stop`,
    `docker compose run --rm --no-deps we-quota python -m app.probe reset`, `docker compose start`
  - `app.cloud check|deploy-rules|pair|devices|revoke|test-push|simulate-captcha`: Firebase relay
    (`simulate-captcha` sends a fake captcha to the phone and checks the answer; never touches WE)
  - `app.usage [days N | sessions N]`: usage history (state/usage.db, mirrored to /usage)
- Full JSON logs and incidents are in the volume: `docker compose exec we-quota ls state/logs state/incidents`
- Without Docker (a venv in `server/`, development only, never next to the container):
  `python -m app.server [--chrome]`; the old systemd setup is in server/README.md.
- Moving to another machine is a manual hand-over: disable the old one first (server/README.md, "Docker").
