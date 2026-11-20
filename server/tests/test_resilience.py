"""Network-trouble behaviour, from the outage on 2026-11-19 00:39-00:56 UTC (no browser, no network)."""
from __future__ import annotations

import asyncio

import pytest
from playwright.async_api import Error as PlaywrightError

from app.config import Credentials, Settings
from app.errors import PortalChanged, TransientError
from app.login_guard import LoginGuard
from app.poller import Poller
from app.we_client import WeClient

BASE = "https://my.te.eg/echannel/"


@pytest.fixture
def settings(tmp_path):
    return Settings(state_dir=tmp_path)


class FakePage:
    def __init__(self, url: str, goto_error: str | None = None) -> None:
        self.url = url
        self.goto_error = goto_error

    async def goto(self, url, **kw):
        if self.goto_error:
            raise PlaywrightError(self.goto_error)
        self.url = url


class FakeCtx:
    async def cookies(self):
        return [{"name": "indiv_login_token", "value": "x" * 20, "domain": "my.te.eg"}]


def make_client(settings) -> WeClient:
    c = WeClient(settings, Credentials("01500000000", "not-a-real-password"), LoginGuard(settings, "fp"))
    c.ctx = FakeCtx()
    return c


def test_dns_failure_on_session_check_is_transient(settings):
    """00:50:58: Page.goto net::ERR_NAME_NOT_RESOLVED escaped as poller.unexpected."""
    c = make_client(settings)
    c.page = FakePage("chrome-error://chromewebdata/", goto_error="net::ERR_NAME_NOT_RESOLVED")
    with pytest.raises(TransientError, match="ERR_NAME_NOT_RESOLVED"):
        asyncio.run(c.has_session_token())


def test_quota_misses_are_transient_until_escalation(settings):
    """00:39: one slow reload was reported as 'portal changed' and cost a 10 min wait."""
    c = make_client(settings)
    outcome: dict = {"raise": True}

    async def once():
        if outcome["raise"]:
            raise PortalChanged("route /accountoverview did not call queryFreeUnit")
        return {"ok": 1}

    c._fetch_quota_once = once
    for _ in range(settings.quota_miss_escalate - 1):
        with pytest.raises(TransientError, match="miss"):
            asyncio.run(c.fetch_quota_raw())
    with pytest.raises(PortalChanged):
        asyncio.run(c.fetch_quota_raw())
    outcome["raise"] = False
    assert asyncio.run(c.fetch_quota_raw()) == {"ok": 1}
    outcome["raise"] = True  # a success resets the streak
    with pytest.raises(TransientError):
        asyncio.run(c.fetch_quota_raw())


def test_portal_doc_live(settings):
    c = make_client(settings)
    for url, live in ((BASE + "#/accountoverview", True), (BASE + "#/login", False),
                      ("chrome-error://chromewebdata/", False), ("about:blank", False)):
        c.page = FakePage(url)
        assert c.portal_doc_live() is live, url


class StubClient:
    def __init__(self, live: bool, age: float) -> None:
        self.live, self.age = live, age

    def portal_doc_live(self):
        return self.live

    def doc_age_s(self):
        return self.age


@pytest.mark.parametrize("live,age,wanted,expected", [
    (True, 700, 600, 80),     # "broken" backoff after an 11+ min old document -> wake for the reload
    (True, 0, 600, 600),      # fresh document: the backoff fits
    (True, 800, 900, 30),     # already past the deadline: soonest sane retry
    (True, 500, 198, 198),    # normal interval untouched
    (False, 700, 900, 900),   # no portal page (login page / error page): nothing to keep alive
])
def test_sleep_capped_for_keepalive(settings, live, age, wanted, expected):
    p = Poller(settings, StubClient(live, age))
    assert p._keepalive_cap(wanted) == pytest.approx(expected)


def test_keepalive_deadline_leaves_room_before_client_logout(settings):
    # The SPA logs out at document age 900 s (seen twice). Reloads must start before that with
    # room for a slow load (nav timeout), and after the normal keepalive threshold.
    assert settings.doc_max_age_s < settings.keepalive_deadline_s
    assert settings.keepalive_deadline_s + settings.nav_timeout_ms / 1000 < 900


# ---------------------------------------------------------------- boot without network (2026-11-20)
def test_start_without_network_does_not_crash(settings):
    """10:45-11:09 local: DNS was down at boot, has_session_token() raised TransientError out of
    start() and systemd restarted the server every 2 min (FAILURE_MODES #54)."""
    c = make_client(settings)
    c.page = FakePage("chrome-error://chromewebdata/", goto_error="net::ERR_NAME_NOT_RESOLVED")
    assert asyncio.run(c._session_after_start()) is True      # decided by the cookie alone

    class NoCookies(FakeCtx):
        async def cookies(self):
            return []
    c.ctx = NoCookies()
    assert asyncio.run(c._session_after_start()) is False


def test_login_page_unreachable_is_transient_not_an_attempt(settings):
    """No network when a login is due: retry later. Before, the login form 'missing' was reported
    as a portal change (10 min wait + incident). Nothing is submitted, so the guard records nothing."""
    c = make_client(settings)
    c.page = FakePage("chrome-error://chromewebdata/", goto_error="net::ERR_INTERNET_DISCONNECTED")
    with pytest.raises(TransientError, match="ERR_INTERNET_DISCONNECTED"):
        asyncio.run(c.login())
    assert c.guard.d["attempts"] == []


class RecordingPage(FakePage):
    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.calls: list[str] = []

    async def goto(self, url, **kw):
        self.calls.append("goto")
        self.url = url

    async def reload(self, **kw):
        self.calls.append("reload")


@pytest.mark.parametrize("start_url, expected", [
    (BASE + "#/login", ["goto", "reload"]),             # the 2026-11-20 case: form open for 10 min
    (BASE + "#/accountoverview", ["goto", "reload"]),   # any portal page: a hash change isn't a load
    ("about:blank", ["goto"]),                           # fresh browser: goto already loads a new page
    ("chrome-error://chromewebdata/", ["goto"]),
])
def test_login_always_uses_a_fresh_page(settings, start_url, expected):
    """The refused login on 2026-11-20 came from a 10-min-old page; the successful ones from fresh
    pages (FAILURE_MODES #53)."""
    c = make_client(settings)
    c.page = RecordingPage(start_url)
    asyncio.run(c._fresh_login_page())
    assert c.page.calls == expected and c.page.url == BASE + "#/login"


# ---------------------------------------------------------------- pace by the phone's WE data (2026-11-20)
@pytest.mark.parametrize("data_on, expected", [
    (lambda: True, "fast"), (lambda: None, "fast"), (None, "fast"),       # on, never heard, no history
    (lambda: False, "idle"),                                               # off: nothing is being used
])
def test_pace_follows_the_phones_data(settings, data_on, expected):
    p = Poller(settings, make_client(settings), data_on=data_on)
    want = settings.poll_interval_s if expected == "fast" else settings.idle_poll_interval_s
    assert p._pace() == want


def test_pace_question_failing_means_fast(settings):
    def broken():
        raise RuntimeError("history unavailable")
    assert Poller(settings, make_client(settings), data_on=broken)._pace() == settings.poll_interval_s


def test_idle_pace_still_keeps_the_session_alive(settings):
    """Idle must never outlast WE's 15-min logout: the keepalive cap still bounds every sleep."""
    assert settings.idle_poll_interval_s * 1.1 > settings.keepalive_deadline_s  # so the cap is what decides
    p = Poller(settings, make_client(settings), data_on=lambda: False)

    class LivePage:
        url = BASE + "#/accountoverview"
    p.client.page = LivePage()
    p.client._doc_loaded_at = __import__("time").monotonic()          # a fresh document
    assert p._keepalive_cap(settings.idle_poll_interval_s * 1.1) <= settings.keepalive_deadline_s
