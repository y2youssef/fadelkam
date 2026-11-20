"""Usage history (app/history.py): per-day and per-session usage from WE's readings. Offline."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.history import History, local_day

UTC = timezone.utc


def t(h: int, m: int = 0, day: int = 20) -> datetime:
    # January: well before today's clock (an open session counts usage up to now). Cairo is UTC+2.
    return datetime(2026, 1, day, h, m, tzinfo=UTC)


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@pytest.fixture
def h(tmp_path):
    return History(tmp_path / "usage.db")


def test_only_changes_are_stored(h):
    assert h.add_reading(t(9), 3116.11)
    assert not h.add_reading(t(9, 3), 3116.11)          # unchanged: not stored
    assert h.add_reading(t(9, 6), 3116.02)
    assert not h.add_reading(t(9, 1), 3000.0)           # older than the last: ignored
    assert h._count() == 2


def test_daily_used_and_added(h):
    for at, mb in [(t(8), 3200), (t(9), 3150), (t(10), 3100), (t(11), 6250), (t(12), 6200)]:
        h.add_reading(at, mb)
    d = h.daily("2026-01-20")
    assert (d["used_mb"], d["added_mb"], d["changes"], d["last_mb"]) == (150.0, 3150.0, 5, 6200)


def test_days_are_cairo_days(h):
    # 22:30 UTC on the 20th is 00:30 on the 21st in Cairo (UTC+2 in November)
    assert local_day(t(22, 30)) == "2026-01-21"
    h.add_reading(t(21, 0), 3000)                       # 23:00 Cairo, 20th
    h.add_reading(t(22, 30), 2990)                      # 00:30 Cairo, 21st
    assert h.daily("2026-01-21")["used_mb"] == 10.0     # the drop counts on the day it was seen
    assert h.daily("2026-01-20")["used_mb"] == 0.0


def test_session_usage_includes_we_lag_but_not_the_next_session(h):
    h.add_reading(t(9), 3000)
    h.add_session_event(True, ms(t(10)), "phone")
    h.add_reading(t(10, 20), 2980)                      # during the session
    h.add_session_event(False, ms(t(10, 30)), "phone")
    h.add_reading(t(10, 50), 2970)                      # WE's lag: 20 min after it ended, still counts
    h.add_reading(t(11, 30), 2960)                      # an hour later: not this session
    s = h.session_doc(ms(t(10)))
    assert (s["used_mb"], s["open"]) == (30.0, False)

    h.add_session_event(True, ms(t(12)), "phone")
    h.add_session_event(False, ms(t(12, 5)), "phone")
    h.add_session_event(True, ms(t(12, 10)), "phone")   # next session 5 min later: cuts the lag window
    h.add_reading(t(12, 15), 2950)
    assert h.session_doc(ms(t(12)))["used_mb"] == 0.0
    assert h.session_doc(ms(t(12, 10)))["used_mb"] == 10.0


def test_session_events_out_of_order_are_ignored(h):
    assert h.add_session_event(False, ms(t(9)), "phone") is None          # off without on
    assert h.add_session_event(True, ms(t(10)), "phone") == "started"
    assert h.add_session_event(True, ms(t(10, 5)), "phone") is None       # on again: keep the first
    assert h.add_session_event(False, ms(t(9, 59)), "phone") is None      # ends before it started
    assert h.add_session_event(False, ms(t(11)), "phone") == "ended"


def test_pending_mirrors_reading_day_and_open_session(h):
    h.add_reading(t(9), 3000)
    h.take_pending()
    h.add_session_event(True, ms(t(10)), "phone")
    h.add_reading(t(10, 5), 2990)
    p = h.take_pending()
    assert p["readings/2026-01-20/120500"] == 2990     # key in Cairo time
    assert p["daily/2026-01-20"]["used_mb"] == 10.0
    assert p[f"sessions/{ms(t(10))}"]["used_mb"] == 10.0 and p[f"sessions/{ms(t(10))}"]["open"] is True
    assert h.take_pending() == {}
    h.restore_pending(p)                                # a failed upload gives them back
    assert h.take_pending() == p


def test_backfill_from_the_event_logs(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    lines = [{"ts": "2026-01-19T15:18:51+00:00", "event": "quota.reading", "remaining_mb": 3631.15, "total_mb": 6250.0},
             {"ts": "2026-01-19T15:21:51+00:00", "event": "quota.reading", "remaining_mb": 3631.15, "total_mb": 6250.0},
             {"ts": "2026-01-20T09:42:54+00:00", "event": "quota.reading", "remaining_mb": 3116.11, "total_mb": 6250.0},
             {"ts": "2026-01-20T09:43:00+00:00", "event": "login.success"}]
    (logs / "scraper.jsonl.1").write_text("\n".join(json.dumps(x) for x in lines[:2]) + "\n")
    (logs / "scraper.jsonl").write_text("\n".join(json.dumps(x) for x in lines[2:]) + "\nnot json\n")
    h = History(tmp_path / "usage.db", logs_dir=logs)
    assert h._count() == 2                              # the unchanged reading isn't stored twice
    assert h.daily("2026-01-20")["used_mb"] == pytest.approx(515.04)
    assert h.take_pending() == {}                       # the first cloud sync queues everything
    assert h.queue_all() > 0 and "daily/2026-01-19" in h.take_pending()


def test_queue_all_since_only_recent(h):
    h.add_reading(t(9, day=10), 4000)
    h.add_reading(t(9), 3000)
    h.take_pending()
    h.queue_all(since=t(0))
    keys = h.take_pending()
    assert "daily/2026-01-20" in keys and "daily/2026-01-10" not in keys
