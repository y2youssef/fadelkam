"""Session loss -> re-login cooldown (FAILURE_MODES #7 and #45). No browser, no network.

If the owner logging in on the phone kicks the scraper out, an instant re-login would put two
logins about a minute apart, and a login 45 s after another got the account locked for an hour
(#1b). These tests pin the 10 min hold that follows any session WE takes away from us.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.config import Credentials, Settings
from app.errors import LoginBlockedByGuard
from app.login_guard import LoginGuard
from app.logs import iso, utcnow
from app.we_client import WeClient

BASE = "https://my.te.eg/echannel/"


@pytest.fixture
def settings(tmp_path):
    return Settings(state_dir=tmp_path)


def logged_in_guard(settings) -> LoginGuard:
    """A guard with one successful login long enough ago that the min gap doesn't interfere."""
    g = LoginGuard(settings, "fp")
    g.end_attempt(g.begin_attempt(), "success")
    long_ago = iso(utcnow() - timedelta(hours=20))
    g.d["attempts"][-1]["started_at"] = long_ago
    g.d["sessions"][-1]["login_at"] = long_ago
    g._save()
    g.check()  # sanity: a login would be allowed right now
    return g


def test_lost_session_holds_logins_for_ten_minutes(settings):
    g = logged_in_guard(settings)
    g.session_lost("redirected_to_login")
    with pytest.raises(LoginBlockedByGuard) as ei:
        g.check()
    e = ei.value
    assert not e.needs_human                      # it clears by itself
    assert "another device" in str(e)
    wait = (e.retry_at - utcnow()).total_seconds()
    assert settings.session_lost_gap_s - 5 < wait <= settings.session_lost_gap_s
    assert g.status()["relogin_after"] == iso(e.retry_at)


def test_cooldown_survives_restart(settings):
    logged_in_guard(settings).session_lost("api:session")
    with pytest.raises(LoginBlockedByGuard):
        LoginGuard(settings, "fp").check()        # a crash loop can't skip it


def test_cooldown_expires(settings):
    g = logged_in_guard(settings)
    g.session_lost("redirected_to_login")
    g.d["relogin_after"] = iso(utcnow() - timedelta(seconds=1))
    g.check()


def test_lost_session_lifetime_still_recorded(settings):
    g = logged_in_guard(settings)
    g.session_lost("redirected_to_login")
    s = g.d["sessions"][-1]
    assert s["end_reason"] == "redirected_to_login" and s["lifetime_s"] >= 20 * 3600


def test_process_restart_does_not_hold_logins(settings):
    g = logged_in_guard(settings)
    g.end_session("process_restart", expired=False)  # our own doing, nobody else logged in
    g.check()


def test_gap_covers_the_lockout_pattern(settings):
    # Live: 45 s after a login -> locked; 3.5 min+ -> fine. The phone login happens up to one
    # poll interval before we notice, so the hold alone must keep us past the risky window.
    assert settings.session_lost_gap_s >= 3.5 * 60 + 60
    assert settings.session_lost_gap_s >= settings.min_login_gap_s


# ---------------------------------------------------------------- WeClient wiring
class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url

    async def evaluate(self, *_a, **_kw):  # the storage probe in has_session_token
        return False


class FakeCtx:
    def __init__(self, cookie: bool) -> None:
        self.cookie = cookie

    async def cookies(self):
        return [{"name": "indiv_login_token", "value": "x" * 20, "domain": "my.te.eg"}] if self.cookie else []


def make_client(settings, guard: LoginGuard, *, url: str, cookie: bool) -> tuple[WeClient, list]:
    c = WeClient(settings, Credentials("01500000000", "not-a-real-password"), guard)
    c.ctx, c.page = FakeCtx(cookie), FakePage(url)
    logins: list = []

    async def fake_login(code_provider=None):
        guard.check()           # the real login() starts with this, before touching the page
        logins.append(1)
    c.login = fake_login  # type: ignore[method-assign]
    return c, logins


def test_session_gone_between_polls_holds_the_relogin(settings):
    """Logged in at one poll, cookie gone at the next (e.g. the phone logged in meanwhile)."""
    g = logged_in_guard(settings)
    c, logins = make_client(settings, g, url=BASE + "#/accountoverview", cookie=True)
    asyncio.run(c.ensure_logged_in())             # poll N: logged in
    c.ctx.cookie, c.page.url = False, BASE + "#/login"
    with pytest.raises(LoginBlockedByGuard, match="another device"):
        asyncio.run(c.ensure_logged_in())         # poll N+1: no instant re-login
    assert logins == []


def test_redirect_to_login_mid_fetch_holds_the_relogin(settings):
    g = logged_in_guard(settings)
    c, _ = make_client(settings, g, url=BASE + "#/accountoverview", cookie=True)
    asyncio.run(c.ensure_logged_in())
    c._session_lost("redirected_to_login")        # what fetch_via_route does on the redirect
    with pytest.raises(LoginBlockedByGuard):
        g.check()


def test_first_login_of_a_process_is_not_held(settings):
    """Startup without a restored session: nothing was taken from us, log in normally."""
    g = logged_in_guard(settings)
    c, logins = make_client(settings, g, url="about:blank", cookie=False)
    asyncio.run(c.ensure_logged_in())
    assert logins == [1] and g.d.get("relogin_after") is None
