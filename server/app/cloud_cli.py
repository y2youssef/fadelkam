"""Firebase relay CLI. Run from server/ (the documented entry point is still app.cloud):

    python -m app.cloud check          # config, token, DB round trip, rules deployed?, devices
    python -m app.cloud deploy-rules   # upload firebase/database.rules.json
    python -m app.cloud pair           # approve the phone (match the 6-digit code it shows)
    python -m app.cloud devices        # paired phones
    python -m app.cloud revoke <uid>   # unpair a phone (uid prefix is enough)
    python -m app.cloud test-push      # send a test notification to paired phones
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from typing import Any

import httpx

from .cloud import (RULES_FILE, SERVER_TS, CloudConfig, CloudConfigError, CloudError, CloudPublisher, GoogleAuth,
                    Rtdb, load_cloud_config, load_service_account)
from .config import Settings, load_settings
from .hub import Hub
from .logs import event, get_logger, setup_logging

log = get_logger("cloud")


def _ms_age(at: Any) -> str:
    if not isinstance(at, (int, float)):
        return "?"
    s = time.time() - at / 1000
    return f"{s:.0f} s ago" if s < 120 else f"{s / 60:.0f} min ago" if s < 7200 else f"{s / 3600:.1f} h ago"


def _load_rules() -> Any:
    return json.loads(RULES_FILE.read_text(encoding="utf-8"))


def _as_rules(live: Any) -> Any:
    if isinstance(live, str):
        try:
            return json.loads(live)
        except ValueError:
            return None
    return live


async def cmd_check(cfg: CloudConfig, auth: GoogleAuth, db: Rtdb) -> int:
    print(f"project      {cfg.project_id}\ndatabase     {cfg.db_url}\nkey file     {cfg.sa_path}")
    t = time.perf_counter()
    await auth.token()
    print(f"OAuth token  ok ({(time.perf_counter() - t) * 1000:.0f} ms)")
    t = time.perf_counter()
    await db.put("diag/check", {"at": SERVER_TS})
    back = await db.get("diag/check")
    await db.delete("diag")
    print(f"DB write/read/delete  ok ({(time.perf_counter() - t) * 1000:.0f} ms, server time {_ms_age((back or {}).get('at'))})")
    try:
        same = _as_rules(await db.get(".settings/rules")) == _load_rules()
        print(f"rules        {'deployed (matches firebase/database.rules.json)' if same else 'DIFFERENT: run deploy-rules'}")
    except CloudError as e:
        print(f"rules        can't read: {e}")
        same = False
    allowed = await db.get("allowed") or {}
    devices = await db.get("devices") or {}
    pairing = await db.get("pairing") or {}
    print(f"phones       {len(allowed)} paired, {sum(1 for u in allowed if (devices.get(u) or {}).get('fcm'))} with a "
          f"push token, {len(pairing)} pairing request(s)")
    st = await db.get("state") or {}
    print(f"state        heartbeat {_ms_age(st.get('heartbeat_at'))}, status {(st.get('status') or {}).get('state')}, "
          f"reading {(st.get('quota') or {}).get('fetched_at')}")
    return 0 if same else 2


async def cmd_deploy_rules(db: Rtdb) -> int:
    rules = _load_rules()
    await db.put(".settings/rules", rules)
    live = _as_rules(await db.get(".settings/rules"))
    print("rules deployed" if live == rules else "rules uploaded, but the read-back differs (check the console)")
    return 0 if live == rules else 2


async def cmd_pair(db: Rtdb, approve: str | None, label: str | None, wait_s: int) -> int:
    deadline = time.monotonic() + wait_s
    while True:
        pending = await db.get("pairing") or {}
        now = time.time() * 1000
        fresh = {}
        for uid, v in pending.items():
            if isinstance(v, dict) and isinstance(v.get("at"), (int, float)) and now - v["at"] < 600_000:
                fresh[uid] = v
            else:
                await db.delete(f"pairing/{uid}")  # expired or malformed
        if fresh or time.monotonic() > deadline:
            break
        print("waiting for a pairing request (open the app and tap Pair)…", flush=True)
        await asyncio.sleep(3)
    if not fresh:
        print("No pairing request in the last 10 minutes.")
        return 1
    for uid, v in fresh.items():
        print(f"  code {v.get('code')}  phone {v.get('model', '?')}  device {uid[:6]}  ({_ms_age(v.get('at'))})")
    if approve:
        match = [u for u, v in fresh.items() if v.get("code") == approve]
    elif len(fresh) == 1 and sys.stdin.isatty():
        uid, v = next(iter(fresh.items()))
        ok = input(f"Approve the phone showing code {v.get('code')}? [y/N] ").strip().lower() == "y"
        match = [uid] if ok else []
    else:
        print("Run again with --approve <code shown on the phone>.")
        return 1
    if len(match) != 1:
        print("No (single) request with that code. Nothing approved.")
        return 1
    uid = match[0]
    await db.put(f"allowed/{uid}", {"at": SERVER_TS, "label": (label or fresh[uid].get("model") or "phone")[:64]})
    await db.delete(f"pairing/{uid}")
    event(log, logging.WARNING, "cloud.phone_paired", device=uid[:6])
    print(f"Paired device {uid[:6]}. The app continues by itself.")
    return 0


async def cmd_devices(db: Rtdb) -> int:
    allowed = await db.get("allowed") or {}
    devices = await db.get("devices") or {}
    if not allowed:
        print("No paired phones. Run: python -m app.cloud pair")
    for uid, a in allowed.items():
        d = devices.get(uid) or {}
        print(f"{uid[:6]}…  {a.get('label', '?'):<20} paired {_ms_age(a.get('at')):<12} "
              f"push token {'registered ' + _ms_age(d.get('at')) if d.get('fcm') else 'missing'}")
    return 0


async def cmd_revoke(db: Rtdb, prefix: str) -> int:
    allowed = await db.get("allowed") or {}
    match = [u for u in allowed if u.startswith(prefix)]
    if len(prefix) < 4 or len(match) != 1:
        print(f"Need a unique uid prefix of 4+ characters (matches: {len(match)}).")
        return 1
    uid = match[0]
    for p in (f"allowed/{uid}", f"devices/{uid}", f"inbox/{uid}"):
        await db.delete(p)
    event(log, logging.WARNING, "cloud.phone_revoked", device=uid[:6])
    print(f"Revoked {uid[:6]}.")
    return 0


async def cmd_test_push(settings: Settings, cfg: CloudConfig, http: httpx.AsyncClient, auth: GoogleAuth) -> int:
    sent = await CloudPublisher(settings, Hub(settings), cfg, http=http, auth=auth).send_test_push()
    print(f"Sent to {sent} phone(s)." if sent else "No paired phone with a working push token yet.")
    return 0 if sent else 1


async def main_async(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cloud")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "deploy-rules", "devices", "test-push"):
        sub.add_parser(name)
    p = sub.add_parser("pair")
    p.add_argument("--approve", metavar="CODE", help="approve the request showing this code")
    p.add_argument("--label", help="name for this phone")
    p.add_argument("--wait", type=int, default=120, help="seconds to wait for a request")
    r = sub.add_parser("revoke")
    r.add_argument("uid", help="uid prefix (see: devices)")
    args = ap.parse_args(argv)

    s = load_settings()
    setup_logging(s.logs_dir, s.log_level)
    try:
        cfg = load_cloud_config(s)
    except CloudConfigError as e:
        print(f"cloud config: {e}", file=sys.stderr)
        return 2
    if cfg is None:
        print(f"Cloud is off: put the Firebase service-account key at {s.state_dir / 'firebase-service-account.json'} "
              "(or set WE_FIREBASE_SA). See server/README.md, \"Cloud\".", file=sys.stderr)
        return 2
    async with httpx.AsyncClient() as http:
        auth = GoogleAuth(load_service_account(cfg.sa_path), http)
        db = Rtdb(cfg.db_url, auth, http)
        try:
            if args.cmd == "check":
                return await cmd_check(cfg, auth, db)
            if args.cmd == "deploy-rules":
                return await cmd_deploy_rules(db)
            if args.cmd == "pair":
                return await cmd_pair(db, args.approve, args.label, args.wait)
            if args.cmd == "devices":
                return await cmd_devices(db)
            if args.cmd == "revoke":
                return await cmd_revoke(db, args.uid)
            if args.cmd == "test-push":
                return await cmd_test_push(s, cfg, http, auth)
        except CloudError as e:
            print(f"cloud: {e}", file=sys.stderr)
            return 1
    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async(sys.argv[1:])))


if __name__ == "__main__":
    main()
