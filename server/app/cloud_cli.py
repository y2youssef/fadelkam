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
import base64
import json
import logging
import secrets
import struct
import sys
import time
import zlib
from typing import Any

import httpx

from .cloud import (RULES_FILE, SERVER_TS, CloudConfig, CloudConfigError, CloudError, CloudPublisher, GoogleAuth,
                    Rtdb, inbox_items, load_cloud_config, load_service_account)
from .config import Settings, load_settings
from .hub import Hub
from .logs import event, get_logger, iso, setup_logging

log = get_logger("cloud")


def _ms_age(at: Any) -> str:
    if not isinstance(at, (int, float)):
        return "?"
    s = time.time() - at / 1000
    return f"{s:.0f} s ago" if s < 120 else f"{s / 60:.0f} min ago" if s < 7200 else f"{s / 3600:.1f} h ago"


def _push_id(d: Any) -> str | None:
    """What a paired phone can be reached by: 'fid' (current) or 'token' (legacy)."""
    if not isinstance(d, dict):
        return None
    return "fid" if d.get("fid") else "token" if d.get("fcm") else None


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
    print(f"phones       {len(allowed)} paired, {sum(1 for u in allowed if _push_id(devices.get(u)))} with a "
          f"push id, {len(pairing)} pairing request(s)")
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
              f"push {_push_id(d) or 'missing'} {_ms_age(d.get('at')) if _push_id(d) else ''}")
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
    print(f"Sent to {sent} phone(s)." if sent else "No paired phone with a working push id yet.")
    return 0 if sent else 1


# 5x7 dot-matrix digits for the simulated captcha picture (no imaging library needed).
_DIGITS = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
}


def digits_png(code: str, scale: int = 8, margin: int = 16) -> bytes:
    """A grayscale PNG of `code` in dark dot-matrix digits on a light background."""
    w = margin * 2 + len(code) * 6 * scale - scale
    h = margin * 2 + 7 * scale
    px = [[235] * w for _ in range(h)]
    for i, ch in enumerate(code):
        for r, row in enumerate(_DIGITS[ch]):
            for c, bit in enumerate(row):
                if bit == "1":
                    x0, y0 = margin + (i * 6 + c) * scale, margin + r * scale
                    for y in range(y0, y0 + scale):
                        px[y][x0:x0 + scale] = [30] * scale
    raw = b"".join(b"\x00" + bytes(row) for row in px)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def find_answer(event: str, payload: Any, cid: str) -> tuple[str, str] | None:
    """(device uid, code) if this inbox stream event carries an answer to captcha `cid`."""
    if event not in ("put", "patch") or not isinstance(payload, dict):
        return None
    for uid, kind, value in inbox_items(event, payload):
        if kind == "answer" and isinstance(value, dict) and value.get("id") == cid:
            return uid, str(value.get("code"))
    return None


async def cmd_simulate_captcha(settings: Settings, cfg: CloudConfig, http: httpx.AsyncClient, auth: GoogleAuth,
                               db: Rtdb, wait_s: int) -> int:
    """The whole captcha path, phone included, without WE: a picture of a random code is published
    as a captcha, the phone is pushed, and the answer it sends back is checked against the code.
    The running server sees the answer too and ignores it (no real captcha pending)."""
    pub = CloudPublisher(settings, Hub(settings), cfg, http=http, auth=auth)
    code, cid = f"{secrets.randbelow(10_000):04d}", "sim" + secrets.token_hex(3)
    answer: asyncio.Future = asyncio.get_running_loop().create_future()

    async def watch() -> None:
        async for ev, data in db.stream("inbox"):
            hit = find_answer(ev, data, cid)
            if hit and not answer.done():
                answer.set_result(hit)
                return
    watcher = asyncio.create_task(watch())
    try:
        await db.put("challenge", {"id": cid, "kind": "image_captcha", "since": iso(), "timeout_s": wait_s,
                                   "simulated": True, "image_png_b64": base64.b64encode(digits_png(code)).decode()})
        sent = await pub.send_challenge_push(cid, "image_captcha")
        if not sent:
            print("No paired phone with a push id.")
            return 1
        print(f"Simulated captcha {cid} pushed to {sent} phone(s). The picture shows {code}: type it on the phone "
              f"(waiting up to {wait_s // 60} min).", flush=True)
        uid, got = await asyncio.wait_for(answer, wait_s)
    except asyncio.TimeoutError:
        print("No answer arrived.")
        return 1
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        await db.delete("challenge")
    ok = got == code
    print(f"Answer from device {uid[:6]}: {got!r} -> {'correct' if ok else 'WRONG, expected ' + repr(code)}. "
          "Captcha removed from Firebase.")
    return 0 if ok else 2


async def main_async(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cloud")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "deploy-rules", "devices", "test-push"):
        sub.add_parser(name)
    sc = sub.add_parser("simulate-captcha", help="a fake captcha to the phone and back; never touches WE")
    sc.add_argument("--wait", type=int, default=300, help="seconds to wait for the answer")
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
            if args.cmd == "simulate-captcha":
                return await cmd_simulate_captcha(s, cfg, http, auth, db, args.wait)
        except CloudError as e:
            print(f"cloud: {e}", file=sys.stderr)
            return 1
    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async(sys.argv[1:])))


if __name__ == "__main__":
    main()
