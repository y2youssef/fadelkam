"""Cloud relay against an in-memory fake of Firebase (OAuth token endpoint, Realtime Database REST
+ streaming, FCM HTTP v1). No network."""
from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.cloud import (QUOTA_KEYS, RULES_FILE, CloudConfig, CloudConfigError, CloudError, CloudPublisher,
                       GoogleAuth, Rtdb, inbox_items, load_cloud_config, parse_sse)
from app.config import Settings
from app.hub import Hub
from app.logs import REDACT, iso
from app.quota import parse_free_units

HOST = "test-default-rtdb.europe-west1.firebasedatabase.app"
DB = f"https://{HOST}"
REAL = json.loads((Path(__file__).parent / "fixtures" / "free_unit_2026-11-18.json").read_text(encoding="utf-8"))
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SA = {"type": "service_account", "project_id": "test", "private_key_id": "kid1",
      "private_key": KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()).decode(),
      "client_email": "sa@test.iam.gserviceaccount.com", "token_uri": "https://oauth2.test/token"}


def now_ms() -> int:
    return int(time.time() * 1000)


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class FakeFirebase:
    def __init__(self) -> None:
        self.tree: dict = {}
        self.writes: list[tuple[str, str, object]] = []
        self.fcm: list[dict] = []
        self.fcm_status: dict[str, tuple[int, dict]] = {}
        self.tokens_issued = 0
        self.fail_db = 0
        self.streams: list[bytes] = []
        self.redirect_to: str | None = None

    def get(self, path: str):
        node = self.tree
        for p in [x for x in path.split("/") if x]:
            if not isinstance(node, dict) or p not in node:
                return None
            node = node[p]
        return node

    def _sv(self, v):
        if isinstance(v, dict):
            if v == {".sv": "timestamp"}:
                return now_ms()
            out = {k: self._sv(x) for k, x in v.items() if x is not None}
            return {k: x for k, x in out.items() if x is not None} or None
        return v

    def set(self, path: str, value) -> None:
        parts = [x for x in path.split("/") if x]
        value = self._sv(value)
        node = self.tree
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        if value is None:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = value

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "oauth2.test":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
            h, c, sig = form["assertion"][0].split(".")
            KEY.public_key().verify(b64d(sig), f"{h}.{c}".encode(), padding.PKCS1v15(), hashes.SHA256())
            claims, header = json.loads(b64d(c)), json.loads(b64d(h))
            assert header == {"alg": "RS256", "typ": "JWT", "kid": "kid1"}
            assert claims["iss"] == SA["client_email"] and claims["aud"] == SA["token_uri"]
            assert "firebase.database" in claims["scope"] and "firebase.messaging" in claims["scope"]
            assert claims["exp"] - claims["iat"] == 3600
            self.tokens_issued += 1
            return httpx.Response(200, json={"access_token": f"ya29.tok{self.tokens_issued}", "expires_in": 3599})
        if url.host == "fcm.googleapis.com":
            assert url.path == "/v1/projects/test/messages:send"
            msg = json.loads(request.content)["message"]
            self.fcm.append(msg)
            assert ("token" in msg) != ("fid" in msg)             # exactly one target
            status, body = self.fcm_status.get(msg.get("token") or msg.get("fid"), (200, {"name": "projects/test/messages/1"}))
            return httpx.Response(status, json=body)
        assert request.headers["authorization"].startswith("Bearer ya29.")
        if self.redirect_to and url.host == HOST:
            target, self.redirect_to = self.redirect_to, None
            return httpx.Response(307, headers={"location": target + url.path})
        path = url.path[: -len(".json")]
        if request.headers.get("accept") == "text/event-stream":
            body = self.streams.pop(0) if self.streams else b""
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
        if self.fail_db:
            self.fail_db -= 1
            return httpx.Response(503, text="Service Unavailable")
        if request.method == "GET":
            return httpx.Response(200, json=self.get(path))
        body = json.loads(request.content) if request.content else None
        self.writes.append((request.method, path, body))
        if request.method == "PUT":
            self.set(path, body)
        elif request.method == "PATCH":
            for k, v in body.items():
                self.set(f"{path}/{k}", v)
        elif request.method == "DELETE":
            self.set(path, None)
        return httpx.Response(204)


@pytest.fixture
def settings(tmp_path):
    return Settings(state_dir=tmp_path)


@pytest.fixture
def fake():
    return FakeFirebase()


def make(settings, fake, **kw):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    cfg = CloudConfig(sa_path=settings.state_dir / "sa.json", db_url=DB, project_id="test",
                      retry_min_s=0.01, retry_max_s=0.05, **kw)
    hub = Hub(settings)
    return hub, CloudPublisher(settings, hub, cfg, http=http, auth=GoogleAuth(SA, http))


def paired(fake, *uids):
    fake.tree["allowed"] = {u: {"at": 1, "label": "phone"} for u in uids}
    fake.tree["devices"] = {u: {"fcm": f"tok-{u}", "at": 1} for u in (*uids, "stranger")}


async def until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------- auth
def test_token_is_a_signed_jwt_and_cached(fake):
    async def go():
        clock = [1_000_000.0]
        http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        auth = GoogleAuth(SA, http, clock=lambda: clock[0])
        assert await auth.token() == "ya29.tok1"
        assert await auth.token() == "ya29.tok1"          # cached
        clock[0] += 3599 - 299                               # inside the 5 min refresh margin
        assert await auth.token() == "ya29.tok2"
        assert REDACT("x ya29.tok2 y") != "x ya29.tok2 y"   # tokens never reach the logs
    asyncio.run(go())


# ---------------------------------------------------------------- what goes up
def test_state_is_whitelisted(settings, fake):
    REDACT.add("01512345678", "phone", keep_last=3)
    async def go():
        hub, pub = make(settings, fake)
        await hub.on_quota(parse_free_units(REAL))
        hub.on_status({"state": "error", "since": iso(), "incident": r"D:\fadelkam\server\state\incidents\x",
                       "error": r"OSError: can't open D:\fadelkam\server\state\a.json for 01512345678"})
        await pub.sync_db()
        st = fake.get("state")
        assert set(st) == {"quota", "status", "heartbeat_at", "heartbeat_s", "poll_interval_s"}
        assert set(st["quota"]) <= set(QUOTA_KEYS) and st["quota"]["remaining_mb"] == 3946.78
        assert "incident" not in st["status"] and "<path>" in st["status"]["error"]
        dump = json.dumps(fake.tree)
        assert "01512345678" not in dump and "01500000000" not in dump and "fadelkam" not in dump
        assert "buckets" not in dump
    asyncio.run(go())


def test_writes_only_on_change_or_heartbeat(settings, fake):
    async def go():
        hub, pub = make(settings, fake)
        await hub.on_quota(parse_free_units(REAL))
        await pub.sync_db()
        await pub.sync_db()
        patches = lambda: [w for w in fake.writes if w[0] == "PATCH" and w[1] == "/state"]
        assert len(patches()) == 1
        hub.quota = {**hub.quota, "fetched_at": iso()}        # a new reading, same numbers
        await pub.sync_db()
        assert len(patches()) == 1
        hub.quota = {**hub.quota, "remaining_mb": 3900.0}     # data was used
        await pub.sync_db()
        assert len(patches()) == 2
        pub._force_write = True                               # heartbeat
        await pub.sync_db()
        assert len(patches()) == 3 and fake.get("state/heartbeat_at") > 0
    asyncio.run(go())


def test_db_outage_is_retried_and_recovers(settings, fake):
    async def go():
        hub, pub = make(settings, fake)
        fake.fail_db = 3
        task = asyncio.create_task(pub._sync_loop())
        await until(lambda: fake.get("state") is not None)
        await until(lambda: pub.health["state"] == "ok")
        task.cancel()
    asyncio.run(go())


def test_stale_challenge_from_a_crash_is_cleared(settings, fake):
    async def go():
        fake.tree["challenge"] = {"id": "old", "kind": "image_captcha"}
        hub, pub = make(settings, fake)
        await pub.sync_db()
        assert fake.get("challenge") is None
    asyncio.run(go())


# ---------------------------------------------------------------- captcha via the cloud
def test_captcha_round_trip(settings, fake):
    async def go():
        paired(fake, "uidA")
        img = settings.state_dir / "challenge" / "captcha.png"
        img.parent.mkdir(parents=True)
        img.write_bytes(b"\x89PNG fake captcha")
        hub, pub = make(settings, fake)
        waiter = asyncio.create_task(hub.code_provider("image_captcha", img))
        await asyncio.sleep(0)
        await pub.sync_db()
        await pub.sync_push()
        ch = fake.get("challenge")
        assert base64.b64decode(ch["image_png_b64"]) == b"\x89PNG fake captcha"
        assert [m["token"] for m in fake.fcm] == ["tok-uidA"]             # not the unpaired device
        assert fake.fcm[0]["android"]["priority"] == "HIGH" and fake.fcm[0]["data"]["type"] == "challenge"
        assert all(isinstance(v, str) for v in fake.fcm[0]["data"].values())
        fake.tree.setdefault("inbox", {})["uidA"] = {"answer": {"id": ch["id"], "code": "mrWw4", "at": now_ms()}}
        await pub.handle_inbox("uidA", "answer", fake.get("inbox/uidA/answer"))
        assert await asyncio.wait_for(waiter, 2) == "mrWw4"
        assert fake.get("inbox/uidA/answer") is None                       # consumed
        await pub.sync_db()
        await pub.sync_push()
        assert fake.get("challenge") is None and len(fake.fcm) == 1        # cleared, no second alert
    asyncio.run(go())


@pytest.mark.parametrize("why", ["stale", "bad code", "malformed"])
def test_inbox_rejects_bad_answers(settings, fake, why):
    async def go():
        hub, pub = make(settings, fake)
        waiter = asyncio.create_task(hub.code_provider("image_captcha", None))
        await asyncio.sleep(0)
        value = {"id": hub.challenge["id"], "code": "mrWw4", "at": now_ms()}  # valid, until we break it
        if why == "stale":
            value["at"] -= 20 * 60_000
        elif why == "bad code":
            value["code"] = "no spaces!"
        else:
            del value["at"]
        await pub.handle_inbox("uidA", "answer", value)
        await asyncio.sleep(0.05)
        assert not waiter.done(), why
        assert ("DELETE", "/inbox/uidA/answer", None) in fake.writes   # removed either way
        waiter.cancel()
    asyncio.run(go())


def test_inbox_stream_end_to_end(settings, fake):
    async def go():
        hub, pub = make(settings, fake)
        ev = {"path": "/", "data": {"uidA": {"refresh": {"at": now_ms()}}}}
        fake.streams.append(f"event: put\ndata: {json.dumps(ev)}\n\n: comment\nevent: keep-alive\ndata: null\n\n"
                            .encode())
        task = asyncio.create_task(pub._inbox_loop())
        await until(hub.refresh_event.is_set)
        await until(lambda: ("DELETE", "/inbox/uidA/refresh", None) in fake.writes)
        task.cancel()
    asyncio.run(go())


def test_inbox_event_shapes():
    a = {"id": "c1", "code": "abc12", "at": 1}
    assert inbox_items("put", {"path": "/", "data": None}) == []
    assert inbox_items("put", {"path": "/", "data": {"u1": {"answer": a}, "u2": {"refresh": {"at": 1}}}}) == [
        ("u1", "answer", a), ("u2", "refresh", {"at": 1})]
    assert inbox_items("put", {"path": "/u1/answer", "data": a}) == [("u1", "answer", a)]
    assert inbox_items("put", {"path": "/u1/answer", "data": None}) == [("u1", "answer", None)]
    assert inbox_items("patch", {"path": "/u1", "data": {"hello": {"at": 1}}}) == [("u1", "hello", {"at": 1})]
    assert inbox_items("put", {"path": "/u1", "data": None}) == []
    assert inbox_items("put", {"path": "/u1/answer/code", "data": "x"}) == [("u1", "answer", {"_partial": True})]


def test_parse_sse():
    async def go():
        async def lines():
            for l in ["event: put", 'data: {"path":"/","data":null}', "", ": c", "event: keep-alive",
                      "data: null", "", "event: cancel", "data: permission denied", ""]:
                yield l
        return [x async for x in parse_sse(lines())]
    assert asyncio.run(go()) == [("put", {"path": "/", "data": None}), ("keep-alive", None),
                                 ("cancel", "permission denied")]


# ---------------------------------------------------------------- push policy
def test_push_policy(settings, fake):
    async def go():
        paired(fake, "uidA")
        hub, pub = make(settings, fake, fcm_quota_min_gap_s=120)
        kinds = lambda: [(m["data"]["type"], m["data"].get("state"), m["android"]["priority"]) for m in fake.fcm]
        for state in ("starting", "ok", "degraded", "broken", "ok", "needs_human"):
            hub.on_status({"state": state, "since": iso()})
            await pub.sync_push()
        assert kinds() == [("status", "broken", "NORMAL"), ("status", "ok", "NORMAL"),
                           ("status", "needs_human", "HIGH")]
        fake.fcm.clear()
        await hub.on_quota(parse_free_units(REAL))
        await pub.sync_push()
        hub.quota = {**hub.quota, "remaining_mb": 3000.0}
        await pub.sync_push()                                   # within 2 min of the last one
        assert [m["data"]["remaining_mb"] for m in fake.fcm] == ["3946.78"]
        assert pub._wake is not None                            # ...so it's scheduled, not dropped
    asyncio.run(go())


def test_clean_stop_is_pushed_to_the_phone(settings, fake):
    """The phone keeps no database connection open (battery), so "stopped" must also go out as a push."""
    async def go():
        paired(fake, "uidA")
        hub, pub = make(settings, fake)
        await pub.close("stopped")
        assert fake.get("state")["status"]["state"] == "stopped"
        assert [(m["data"]["type"], m["data"]["state"], m["android"]["priority"]) for m in fake.fcm] == \
            [("status", "stopped", "NORMAL")]
    asyncio.run(go())


def test_push_goes_to_the_installation_id(settings, fake):
    """The app registers its Firebase installation ID (firebase-messaging 25.1.0+). A phone that
    only has a legacy token still gets its pushes by token."""
    async def go():
        fake.tree["allowed"] = {"uidF": {"at": 1}, "uidT": {"at": 1}}
        fake.tree["devices"] = {"uidF": {"fid": "fid-uidF", "at": 1}, "uidT": {"fcm": "tok-uidT", "at": 1},
                                "stranger": {"fid": "fid-x", "at": 1}}
        hub, pub = make(settings, fake)
        assert await pub.send_test_push() == 2
        targets = sorted((("fid", m["fid"]) if "fid" in m else ("token", m["token"])) for m in fake.fcm)
        assert targets == [("fid", "fid-uidF"), ("token", "tok-uidT")]   # never the unpaired one
    asyncio.run(go())


def test_dead_installation_id_is_dropped(settings, fake):
    """FCM answers an unknown FID with 404 NotRegistered (checked live with validate_only)."""
    async def go():
        fake.tree["allowed"] = {"uidF": {"at": 1}}
        fake.tree["devices"] = {"uidF": {"fid": "fid-uidF", "at": 1}}
        fake.fcm_status["fid-uidF"] = (404, {"error": {"code": 404, "message": "NotRegistered", "status": "NOT_FOUND"}})
        hub, pub = make(settings, fake)
        assert await pub.send_test_push() == 0
        assert fake.get("devices/uidF") is None
    asyncio.run(go())


def test_phone_session_events_reach_the_history_and_are_cleared(settings, fake):
    """WE mobile data on/off from the phone: recorded even when old (laptop was off), then deleted."""
    from app.history import History
    from app.cloud import inbox_items

    async def go():
        hist = History(settings.state_dir / "usage.db")
        hub = Hub(settings, history=hist)
        hub_pub = make(settings, fake)[1]
        hub_pub.hub = hub
        start_ms = int(time.time() * 1000) - 3 * 3600 * 1000           # 3 h ago: not "stale" for history
        fake.tree["inbox"] = {"uidA": {"session": {"-k1": {"on": True, "t": start_ms, "at": 1},
                                                   "-k2": {"on": False, "t": start_ms + 600_000, "at": 1}}}}
        for uid, kind, value in inbox_items("put", {"path": "/", "data": fake.tree["inbox"]}):
            await hub_pub.handle_inbox(uid, kind, value)
        assert hist.session_doc(start_ms)["end"] is not None
        assert fake.get("inbox/uidA/session/-k1") is None and fake.get("inbox/uidA/session/-k2") is None
        # one event arriving on its own path, as the live stream sends it
        items = inbox_items("put", {"path": "/uidA/session/-k3", "data": {"on": True, "t": start_ms + 3_600_000, "at": 1}})
        assert items == [("uidA", "session", {"-k3": {"on": True, "t": start_ms + 3_600_000, "at": 1}})]
    asyncio.run(go())


def test_usage_is_mirrored_to_firebase(settings, fake):
    from app.history import History

    async def go():
        hist = History(settings.state_dir / "usage.db")
        hub, pub = make(settings, fake)
        hub.history = hist
        hist.add_reading(datetime(2026, 11, 20, 9, tzinfo=timezone.utc), 3000.0)
        hist.add_reading(datetime(2026, 11, 20, 10, tzinfo=timezone.utc), 2990.0)
        await pub.sync_db()
        assert fake.get("usage/daily/2026-11-20")["used_mb"] == 10.0
        assert fake.get("usage/readings/2026-11-20/120000") == 2990.0
        assert (settings.state_dir / "usage_cloud_synced").exists()
    asyncio.run(go())


def test_dead_push_token_is_dropped(settings, fake):
    async def go():
        paired(fake, "uidA")
        fake.fcm_status["tok-uidA"] = (404, {"error": {"status": "NOT_FOUND",
                                                       "details": [{"errorCode": "UNREGISTERED"}]}})
        hub, pub = make(settings, fake)
        hub.on_status({"state": "needs_human", "since": iso()})
        await pub.sync_push()
        assert fake.get("devices/uidA") is None and fake.get("allowed/uidA") is not None
    asyncio.run(go())


def test_push_failure_doesnt_block_db_writes(settings, fake):
    async def go():
        paired(fake, "uidA")
        fake.fcm_status["tok-uidA"] = (503, {"error": {"status": "UNAVAILABLE"}})
        hub, pub = make(settings, fake)
        hub.on_status({"state": "needs_human", "since": iso()})
        await pub.sync_db()
        await pub.sync_push()                                   # fails, schedules a retry
        assert fake.get("state/status/state") == "needs_human" and pub._push_fails == 1
    asyncio.run(go())


# ---------------------------------------------------------------- transport details
def test_redirects_keep_the_token_only_within_firebase(settings, fake):
    async def go():
        http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        db = Rtdb(DB, GoogleAuth(SA, http), http)
        fake.tree["x"] = 1
        fake.redirect_to = "https://s-euw1-1.europe-west1.firebasedatabase.app"
        assert await db.get("x") == 1
        fake.redirect_to = "https://evil.example.com"
        with pytest.raises(CloudError, match="refusing redirect"):
            await db.get("x")
    asyncio.run(go())


def test_close_marks_the_server_stopped(settings, fake):
    async def go():
        hub, pub = make(settings, fake)
        await pub.close("stopped")
        assert fake.get("state/status/state") == "stopped"
    asyncio.run(go())


def test_broken_listener_doesnt_break_the_hub(settings):
    hub = Hub(settings)
    hub.add_listener(lambda kind: 1 / 0)
    hub.on_status({"state": "ok"})  # must not raise
    assert hub.status["state"] == "ok"


# ---------------------------------------------------------------- config + rules
def test_config_loading(settings, monkeypatch):
    for k in ("WE_CLOUD", "WE_FIREBASE_SA", "WE_FIREBASE_DB_URL"):
        monkeypatch.delenv(k, raising=False)
    assert load_cloud_config(settings) is None                           # no key: cloud off
    monkeypatch.setenv("WE_CLOUD", "1")
    with pytest.raises(CloudConfigError):
        load_cloud_config(settings)
    monkeypatch.delenv("WE_CLOUD")
    (settings.state_dir / "firebase-service-account.json").write_text(json.dumps(SA))
    cfg = load_cloud_config(settings)
    assert cfg.db_url == DB and cfg.project_id == "test"
    monkeypatch.setenv("WE_FIREBASE_DB_URL", "https://evil.example.com")
    with pytest.raises(CloudConfigError):
        load_cloud_config(settings)
    (settings.state_dir / "firebase-service-account.json").write_text('{"type": "user"}')
    monkeypatch.delenv("WE_FIREBASE_DB_URL")
    with pytest.raises(CloudConfigError):
        load_cloud_config(settings)


def test_rules_deny_by_default_and_scope_phone_writes():
    rules = json.loads(RULES_FILE.read_text(encoding="utf-8"))["rules"]
    assert rules[".read"] is False and rules[".write"] is False
    for node in ("state", "challenge"):
        assert ".write" not in rules[node] and "allowed" in rules[node][".read"]
    for node in ("devices", "inbox"):
        w = rules[node]["$uid"][".write"]
        assert "auth.uid === $uid" in w and "root.child('allowed').child($uid).exists()" in w
    assert "!root.child('allowed')" in rules["pairing"]["$uid"][".write"]
    assert rules["inbox"]["$uid"]["$other"] == {".validate": False}
    assert ".write" not in rules["allowed"]["$uid"]                     # only the laptop pairs phones


# ---------------------------------------------------------------- simulate-captcha (CLI)
def test_simulated_captcha_picture_is_a_valid_png():
    import struct
    import zlib
    from app.cloud_cli import digits_png
    png = digits_png("4071")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", png[16:24])
    idat_len = struct.unpack(">I", png[33:37])[0]
    assert len(zlib.decompress(png[41:41 + idat_len])) == h * (w + 1)   # one filter byte per row


def test_simulated_captcha_answer_is_matched_by_id():
    from app.cloud_cli import find_answer
    put = {"path": "/uidA/answer", "data": {"id": "sim123", "code": "4071", "at": 1}}
    assert find_answer("put", put, "sim123") == ("uidA", "4071")
    assert find_answer("put", put, "simOTHER") is None                         # another captcha
    assert find_answer("put", {"path": "/", "data": {"uidA": {"answer": put["data"]}}}, "sim123") == ("uidA", "4071")
    assert find_answer("keep-alive", None, "sim123") is None
