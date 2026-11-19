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
