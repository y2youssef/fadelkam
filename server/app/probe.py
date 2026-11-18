"""Manual recon / ops CLI.  Run from the `server/` directory:

    python -m app.probe creds                 # parse creds (no network); prints masked number + warnings
    python -m app.probe status                # login-guard state, recent attempts, session lifetimes
    python -m app.probe reset [--clear-lock]  # clear a hard stop after a human checked the cause
    python -m app.probe page                  # load the login page only (no login): WAF / selectors / build id
    python -m app.probe login [--headed]      # ONE guarded login (+captcha prompt), then map SPA routes to
                                              # the API calls they make, and save a probe bundle
    python -m app.probe session               # is the saved browser session still logged in? (lifetime test)

Every command logs to state/logs/; failures leave a folder in state/incidents/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

from .config import Settings, load_credentials, load_settings
from .diagnostics import save_incident
from .errors import WeError
from .login_guard import LoginGuard
from .logs import REDACT, event, get_logger, setup_logging
from .we_client import SEL, WeClient

log = get_logger("probe")

# SPA routes found in the portal bundle that plausibly show usage / remaining quota.
# /home last: in run 2 the session died right as we opened it (cause was the anti-debug script).
CANDIDATE_ROUTES = ["/accountoverview", "/usage", "/overview", "/homemobile", "/balance-detail", "/home"]
QUOTA_HINTS = ("FreeUnit", "Usage", "usage", "Balance", "Quota", "quota", "Bundle", "Offering")


async def terminal_code_provider(kind: str, image: Path | None) -> str | None:
    """Get the code from a human: typed in an interactive terminal, or written to
    state/challenge/code.txt (works when the probe runs without a keyboard)."""
    print(f"\n>>> WE is asking for a {kind}.", flush=True)
    if image:
        print(f">>> Captcha image: {image}", flush=True)
        if os.name == "nt":
            try:
                os.startfile(image)  # type: ignore[attr-defined]  # pops it open on the desktop
            except OSError:
                pass
    code_file = (image.parent if image else Path("state/challenge")) / "code.txt"
    code_file.unlink(missing_ok=True)
    if sys.stdin and sys.stdin.isatty():
        try:
            code = await asyncio.to_thread(input, ">>> Type the code (empty = give up): ")
            return code.strip() or None
        except EOFError:  # "tty" without a keyboard (e.g. run from a tool) -> use the file
            pass
    print(f">>> Write the code into {code_file.resolve()} (waiting up to 10 min)", flush=True)
    for _ in range(600):
        if code_file.exists():
            code = code_file.read_text(encoding="utf-8").strip()
            code_file.unlink(missing_ok=True)
            return code or None
        await asyncio.sleep(1)
    return None


async def cmd_page(s: Settings, client: WeClient) -> int:
    await client.page.goto(s.login_url, wait_until="domcontentloaded")
    try:
        await client.page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass
    found = {k: await client.page.locator(v).count() for k, v in SEL.items()}
    ua = await client.page.evaluate("navigator.userAgent")
    wd = await client.page.evaluate("navigator.webdriver")
    out = {"url": client.page.url, "selectors_found": found, "portal_build": client.recorder.portal_build,
           "api_calls": len(client.recorder.calls), "user_agent": ua, "webdriver": wd,
           "deviceid": client.recorder.device_id}
    print(json.dumps(out, indent=1))
    return 0 if all(found.values()) else 2


async def _settle(client: WeClient, extra_ms: int = 4000) -> None:
    try:
        await client.page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass
    await client.page.wait_for_timeout(extra_ms)


def _report(client: WeClient, label: str, start: int, route_map: dict) -> None:
    calls = list(client.recorder.calls)[start:]
    route_map[label] = [f"{c['endpoint']} [{c['ret_code']}]" for c in calls]
    hits = [c for c in route_map[label] if any(h in c for h in QUOTA_HINTS)]
    print(f"{label:<18} -> {client.page.url.split('#', 1)[-1]:<22} {len(calls):>3} calls; quota-ish: {hits}", flush=True)


async def cmd_login(s: Settings, client: WeClient, routes: list[str]) -> int:
    start = len(client.recorder.calls)
    await client.ensure_logged_in(code_provider=terminal_code_provider)
    route_map: dict[str, list[str]] = {}
    await _settle(client, 6000)  # let the post-login landing page load everything it wants
    _report(client, "(landing)", start, route_map)
    for route in routes:
        start = len(client.recorder.calls)
        try:
            await client.page.goto(s.route_url(route), wait_until="domcontentloaded")
        except Exception as e:
            event(log, logging.WARNING, "probe.route_error", route=route, error=str(e)[:200])
        await _settle(client, 2500)
        _report(client, route, start, route_map)
    storage = await client.storage_snapshot()
    cookies = [{"name": c["name"], "httpOnly": c["httpOnly"], "persistent": c["expires"] > 0,
                "domain": c["domain"]} for c in await client.ctx.cookies()]
    bundle = await save_incident(s, "probe", page=client.page, recorder=client.recorder,
                                 extra={"route_map": route_map, "storage_keys": storage, "cookies": cookies,
                                        "antidebug": {"stubbed": client.antidebug_stubbed,
                                                      "hits": client.antidebug_hits},
                                        "guard": client.guard.status()})
    print(f"\nprobe bundle: {bundle}\nantidebug stubbed={client.antidebug_stubbed} hits={client.antidebug_hits}"
          f"\ncookies: {json.dumps(cookies)}\nsession storage keys: {storage['session']}", flush=True)
    return 0


async def cmd_session(s: Settings, client: WeClient) -> int:
    await client.page.goto(s.base_url, wait_until="domcontentloaded")
    await client.page.wait_for_timeout(4000)
    token = await client.has_session_token()
    on_login = "#/login" in client.page.url
    print(json.dumps({"url": client.page.url, "token_key_present": token, "on_login_page": on_login,
                      "guard_sessions": client.guard.status()["session_lifetimes_s"]}, indent=1))
    try:
        body = await client.fetch_quota_raw()
        print("quota call OK; top-level keys:", list(body))
    except WeError as e:
        print("quota call failed:", e.as_dict())
    return 0


async def main_async(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.probe")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("creds", "status", "page", "session"):
        sub.add_parser(name)
    r = sub.add_parser("reset")
    r.add_argument("--clear-lock", action="store_true", help="also forget WE's lock time (only if you are sure)")
    lg = sub.add_parser("login")
    lg.add_argument("--headed", action="store_true", help="show the browser window")
    lg.add_argument("--routes", default=",".join(CANDIDATE_ROUTES))
    for p in sub.choices.values():
        p.add_argument("--chrome", action="store_true", help="use the installed Google Chrome")
    args = ap.parse_args(argv)

    s = load_settings()
    if getattr(args, "headed", False):
        s = replace(s, headless=False)
    if getattr(args, "chrome", False):
        s = replace(s, browser_channel="chrome")
    setup_logging(s.logs_dir, s.log_level)
    creds = load_credentials()
    REDACT.add(creds.password, "password")
    REDACT.add(creds.phone, "phone", keep_last=3)
    guard = LoginGuard(s, creds.fingerprint)

    if args.cmd == "creds":
        print(REDACT(json.dumps({"phone": creds.phone, "password_length": len(creds.password),
                                 "fingerprint": creds.fingerprint, "warnings": creds.warnings()}, indent=1)))
        return 0
    if args.cmd == "status":
        print(json.dumps(guard.status(), indent=1))
        return 0
    if args.cmd == "reset":
        guard.reset(clear_lock=args.clear_lock)
        print(json.dumps(guard.status(), indent=1))
        return 0

    for w in creds.warnings():
        event(log, logging.WARNING, "creds.warning", warning=w)
    async with WeClient(s, creds, guard) as client:
        try:
            if args.cmd == "page":
                return await cmd_page(s, client)
            if args.cmd == "login":
                return await cmd_login(s, client, [x.strip() for x in args.routes.split(",") if x.strip()])
            if args.cmd == "session":
                return await cmd_session(s, client)
        except WeError as e:
            print(REDACT(json.dumps(e.as_dict(), indent=1)), file=sys.stderr)
            return 1
    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async(sys.argv[1:])))


if __name__ == "__main__":
    main()
