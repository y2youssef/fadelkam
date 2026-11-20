"""Offline tests for the parts that protect the account and the secrets (no network)."""
from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from datetime import timedelta

import pytest

from app.config import Credentials, Settings, _parse_creds_text
from app.errors import (AccountLocked, ChallengeRequired, LoginBlockedByGuard, LoginRejected,
                        classify_login_failure, parse_lock_minutes)
from app.files import write_private
from app.hub import CODE_RE, code_file, take_code_file
from app.login_guard import LoginGuard
from app.logs import Redactor, utcnow
from app.we_client import WeClient


# ---------------------------------------------------------------- credentials
@pytest.mark.parametrize("text", [
    "phone: 01512345678\npassword: p@ss:word#1",
    "﻿Phone : 01512345678\r\nPassword: p@ss:word#1\r\n",
    "01512345678\np@ss:word#1\n",
    "password: p@ss:word#1\nphone: +201512345678",
])
def test_creds_formats(text):
    c = _parse_creds_text(text)
    assert c.phone == "01512345678"
    assert c.password == "p@ss:word#1"          # ':' inside a password survives
    assert "p@ss" not in repr(c)                 # never in repr


def test_creds_warnings():
    assert Credentials("01512345678", "x").warnings() == []
    assert "015" in Credentials("01012345678", "x").warnings()[0]
    assert "11-digit" in Credentials("0223456789", "x").warnings()[0]


def test_creds_incomplete():
    with pytest.raises(ValueError):
        _parse_creds_text("phone: 01512345678")


# ---------------------------------------------------------------- classification
def test_live_lockout_message_is_account_locked():
    # verbatim response seen on 2026-11-18
    err = classify_login_failure({"retCode": "9999", "errorNo": "60301025072100002",
                                  "errorMsg": "You have reached the maximum number of incorrect attempts. "
                                              "Please try again after one hour."})
    assert isinstance(err, AccountLocked) and err.lock_minutes == 60 and err.error_no == "60301025072100002"


def test_unknown_refusal_is_conservative():
    err = classify_login_failure({"retCode": "1", "errorNo": "999", "errorMsg": "Something odd"})
    assert type(err) is LoginRejected


def test_sms_message_is_challenge():
    err = classify_login_failure({"retCode": "1", "errorMsg": "Please enter the verification code sent by SMS"})
    assert isinstance(err, ChallengeRequired) and err.challenge == "sms"


@pytest.mark.parametrize("msg,mins", [("try again after one hour", 60), ("try again after 15 minutes", 15),
                                      ("try again after 2 hours", 120), ("no duration", None)])
def test_parse_lock_minutes(msg, mins):
    assert parse_lock_minutes(msg) == mins


# ---------------------------------------------------------------- guard
@pytest.fixture
def settings(tmp_path):
    return Settings(state_dir=tmp_path)


def test_attempt_persisted_before_submit(settings):
    g = LoginGuard(settings, "fp")
    g.begin_attempt()
    on_disk = json.loads(settings.ledger_path.read_text())
    assert on_disk["attempts"][0]["outcome"] == "pending"   # a crash right now still counts


def test_lock_blocks_and_hard_stops(settings):
    g = LoginGuard(settings, "fp")
    a = g.begin_attempt()
    g.end_attempt(a, "locked", AccountLocked("locked", lock_minutes=60))
    with pytest.raises(LoginBlockedByGuard) as ei:
        LoginGuard(settings, "fp").check()                  # survives restart
    assert ei.value.needs_human


def test_lock_without_hard_stop_waits_out_the_lock(settings):
    s = replace(settings, hard_stop_on_lock=False)
    g = LoginGuard(s, "fp")
    a = g.begin_attempt()
    g.end_attempt(a, "locked", AccountLocked("locked", lock_minutes=60))
    with pytest.raises(LoginBlockedByGuard) as ei:
        g.check()
    assert not ei.value.needs_human and ei.value.retry_at > utcnow() + timedelta(minutes=60)


def test_two_rejections_hard_stop_until_creds_change(settings):
    settings = replace(settings, min_login_gap_s=0)
    g = LoginGuard(settings, "fp")
    for _ in range(2):
        g.end_attempt(g.begin_attempt(), "rejected", LoginRejected("bad"))
    with pytest.raises(LoginBlockedByGuard):
        g.check()
    LoginGuard(settings, "new-fp").check()                  # new credentials clear the stop


def test_min_gap_between_logins(settings):
    g = LoginGuard(settings, "fp")
    g.end_attempt(g.begin_attempt(), "success")
    with pytest.raises(LoginBlockedByGuard) as ei:
        g.check()                                           # 45 s later was refused live
    assert "min gap" in str(ei.value) and ei.value.retry_at > utcnow() + timedelta(minutes=9)


def test_hourly_cap(settings):
    g = LoginGuard(replace(settings, max_logins_per_hour=2, min_login_gap_s=0), "fp")
    for _ in range(2):
        g.end_attempt(g.begin_attempt(), "success")
    with pytest.raises(LoginBlockedByGuard) as ei:
        g.check()
    assert "cap" in str(ei.value)


def test_transient_backoff_grows(settings):
    g = LoginGuard(settings, "fp")
    g.end_attempt(g.begin_attempt(), "transient")
    first = g.d["backoff_until"]
    g.d["backoff_until"] = None
    g.end_attempt(g.begin_attempt(), "transient")
    assert g.d["backoff_until"] > first


def test_unanswered_challenges(settings):
    g = LoginGuard(replace(settings, max_logins_per_hour=99), "fp")
    for i in range(3):
        g.d["backoff_until"] = None
        g.end_attempt(g.begin_attempt(), "challenge", ChallengeRequired("c", challenge="image_captcha"))
    assert g.d["hard_stop"] is not None


def test_corrupt_ledger_fails_closed(settings):
    settings.ledger_path.write_text("{not json")
    with pytest.raises(LoginBlockedByGuard):
        LoginGuard(settings, "fp").check()


def test_session_lifetime_recorded(settings):
    g = LoginGuard(settings, "fp")
    g.end_attempt(g.begin_attempt(), "success")
    assert g.end_session("expired") is not None
    assert g.status()["session_lifetimes_s"]["count"] == 1


def test_restored_session_resumes_original_login_time(settings):
    g = LoginGuard(settings, "fp")
    g.end_attempt(g.begin_attempt(), "success")
    login_at = g.d["sessions"][-1]["login_at"]
    g.end_session("process_restart", expired=False)
    g.resume_session()
    assert g.d["sessions"][-1]["ended_at"] is None and g.d["sessions"][-1]["login_at"] == login_at


def test_restart_closed_sessions_excluded_from_stats(settings):
    g = LoginGuard(settings, "fp")
    g.end_attempt(g.begin_attempt(), "success")
    g.end_session("process_restart", expired=False)
    assert g.status()["session_lifetimes_s"]["count"] == 0


# ---------------------------------------------------------------- redaction
def test_redactor_phone_variants_and_password():
    r = Redactor()
    r.add("01012345678", "phone", keep_last=3)
    r.add("s3cret#pass", "password")
    out = r('{"msisdn":"201012345678","id":"1012345678","x":"01012345678","p":"s3cret#pass"}')
    assert "1012345678" not in out and "s3cret" not in out
    assert "<phone…678>" in out and "<password>" in out


# ---------------------------------------------------------------- private state files
@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_write_private_is_owner_only_and_atomic(tmp_path):
    path = tmp_path / "new_dir" / "login_ledger.json"
    write_private(path, '{"a": 1}')
    assert json.loads(path.read_text()) == {"a": 1}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    path.chmod(0o644)                              # e.g. written by an older version
    (path.parent / (path.name + ".tmp")).write_text("left by a crash")
    (path.parent / (path.name + ".tmp")).chmod(0o644)
    write_private(path, '{"a": 2}')
    assert json.loads(path.read_text()) == {"a": 2}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["login_ledger.json"]


def test_guard_ledger_and_incidents_are_private(settings):
    g = LoginGuard(settings, "fp")
    g.end_attempt(g.begin_attempt(), "success")
    if os.name == "posix":
        assert stat.S_IMODE(settings.ledger_path.stat().st_mode) == 0o600


def test_code_file_rules(tmp_path):
    path = code_file(tmp_path)
    assert take_code_file(path) is None            # nothing written yet: keep waiting
    path.parent.mkdir(parents=True)
    path.write_text(" mrWw4 \n")
    assert take_code_file(path) == "mrWw4" and not path.exists()
    path.write_text("\n")
    assert take_code_file(path) == ""              # empty = the operator gives up
    assert CODE_RE.match("mrWw4") and not CODE_RE.match("12") and not CODE_RE.match("a b c")


@pytest.mark.skipif(os.name != "posix", reason="flock")
def test_one_client_per_state_dir(settings):
    """A probe started next to the running server must refuse, not open a second browser on the
    same profile with its own copy of the login ledger."""
    creds = Credentials("01512345678", "pw-for-tests")
    a = WeClient(settings, creds, LoginGuard(settings, creds.fingerprint))
    b = WeClient(settings, creds, LoginGuard(settings, creds.fingerprint))
    a._take_instance_lock()
    with pytest.raises(SystemExit, match=f"pid {os.getpid()}"):
        b._take_instance_lock()
    a._release_instance_lock()
    b._take_instance_lock()                        # free again once the first one has closed
    b._release_instance_lock()


@pytest.mark.parametrize("text", [
    "phone: 01512345678\npasword: s3cret value",
    "phone: 01512345678\nPassowrd: s3cret",
    "pass word: s3cret\n01512345678",
])
def test_creds_misspelt_label_refused(text):
    """Seen 2026-11-19: a misspelt label made the whole line the password and WE refused the login."""
    with pytest.raises(ValueError, match=r"creds line \d") as e:
        _parse_creds_text(text)
    assert "s3cret" not in str(e.value) and "pas" not in str(e.value).split("label")[0]


@pytest.mark.skipif(os.name != "posix", reason="flock")
def test_probe_reset_refuses_while_the_service_runs(tmp_path, monkeypatch):
    """The running server keeps its own copy of the ledger; a reset underneath it would be
    overwritten or race its next write (FAILURE_MODES #47)."""
    import asyncio
    from app.files import InstanceLock
    from app.probe import main_async
    monkeypatch.setenv("WE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("WE_PHONE", "01512345678")
    monkeypatch.setenv("WE_PASSWORD", "pw-for-tests")
    service = InstanceLock(tmp_path)
    service.acquire()
    with pytest.raises(SystemExit, match=f"pid {os.getpid()}"):
        asyncio.run(main_async(["reset"]))
    service.release()
    assert asyncio.run(main_async(["reset"])) == 0      # stopped service: reset works
