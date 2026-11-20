"""Usage history: each change of the WE reading, per-day totals and per-session usage.

Readings come from the poller (and, the first time, from the event logs, which go back to the
laptop's first reading). Sessions are the phone's "WE mobile data on -> off" periods, reported
through Firebase (/inbox/<uid>/session). Stored locally in SQLite (state/usage.db; with sqlite3:
`SELECT * FROM daily`), and mirrored to Firebase under /usage for the console and the phone:

    /usage/readings/<YYYY-MM-DD>/<HHMMSS>  remaining MB, only when the number changed
    /usage/daily/<YYYY-MM-DD>              {used_mb, added_mb, changes, last_mb, updated_at}
    /usage/sessions/<start_ms>             {start, end, used_mb, open}

Days are Cairo local days. "Used" sums the drops between consecutive readings; a rise (bundle
renewal or top-up) counts as "added", never as negative usage. WE's portal lags real usage, so a
session also gets the drops seen up to LAG_S after it ended (data was off by then, so that usage
happened during the session), unless the next session starts first.

Never in the way of the scraper: callers wrap these calls, and the store is a local file.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .logs import event, get_logger, iso, utcnow

log = get_logger("history")

CAIRO = ZoneInfo("Africa/Cairo")
LAG_S = 30 * 60
SAME_MB = 0.005  # readings closer than this are "unchanged"

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts TEXT PRIMARY KEY,            -- UTC, ISO 8601
    remaining_mb REAL NOT NULL,
    total_mb REAL,
    offer TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    start_ms INTEGER PRIMARY KEY,   -- phone clock, ms since epoch
    end_ms INTEGER,                 -- NULL while mobile data is still on
    device TEXT
);
-- Ad-hoc queries (sqlite3 state/usage.db): drops between readings, and per-day totals.
CREATE VIEW IF NOT EXISTS changes AS
    SELECT ts, remaining_mb,
           LAG(remaining_mb) OVER (ORDER BY ts) - remaining_mb AS delta_mb
    FROM readings;
CREATE VIEW IF NOT EXISTS daily AS
    SELECT date(ts, 'localtime') AS day,
           ROUND(SUM(CASE WHEN delta_mb > 0 THEN delta_mb ELSE 0 END), 2) AS used_mb,
           ROUND(SUM(CASE WHEN delta_mb < 0 THEN -delta_mb ELSE 0 END), 2) AS added_mb,
           COUNT(*) AS changes
    FROM changes GROUP BY day ORDER BY day;
"""


def local_day(t: datetime) -> str:
    return t.astimezone(CAIRO).date().isoformat()


def _key(t: datetime) -> str:
    """Timestamps are stored and compared as UTC ISO text, which sorts correctly only if every
    value has the same offset (a Cairo +02:00 time compared as text picks the wrong reading)."""
    return iso(t.astimezone(timezone.utc))


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


class History:
    def __init__(self, path: Path, logs_dir: Path | None = None) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        path.chmod(0o600)
        self._pending: dict[str, Any] = {}
        if logs_dir is not None and self._count() == 0:
            self.backfill(logs_dir)

    # ------------------------------------------------------------------ readings
    def _count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

    def last(self) -> tuple[datetime, float] | None:
        row = self.db.execute("SELECT ts, remaining_mb FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
        return (datetime.fromisoformat(row[0]), row[1]) if row else None

    def add_reading(self, at: datetime, remaining_mb: float, total_mb: float | None = None,
                    offer: str | None = None) -> bool:
        """Store a reading if the number changed. Returns True if it was stored."""
        last = self.last()
        if last and (at <= last[0] or abs(last[1] - remaining_mb) < SAME_MB):
            return False
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO readings VALUES (?, ?, ?, ?)",
                            (_key(at), round(remaining_mb, 2), total_mb, offer))
        day = local_day(at)
        self._pending[f"readings/{day}/{at.astimezone(CAIRO):%H%M%S}"] = round(remaining_mb, 2)
        self._pending[f"daily/{day}"] = self.daily(day)
        for start_ms in self._sessions_touching(at):
            self._pending[f"sessions/{start_ms}"] = self.session_doc(start_ms)
        return True

    def _readings(self, since: datetime | None = None, until: datetime | None = None) -> list[tuple[datetime, float]]:
        q, args = "SELECT ts, remaining_mb FROM readings", []
        if since is not None:
            q += " WHERE ts >= ?"
            args.append(_key(since))
        if until is not None:
            q += (" AND" if args else " WHERE") + " ts <= ?"
            args.append(_key(until))
        return [(datetime.fromisoformat(t), mb) for t, mb in self.db.execute(q + " ORDER BY ts", args)]

    def _before(self, t: datetime) -> tuple[datetime, float] | None:
        row = self.db.execute("SELECT ts, remaining_mb FROM readings WHERE ts < ? ORDER BY ts DESC LIMIT 1",
                              (_key(t),)).fetchone()
        return (datetime.fromisoformat(row[0]), row[1]) if row else None

    @staticmethod
    def _drops(series: Iterable[tuple[datetime, float]], prev: float | None) -> tuple[float, float, int]:
        used = added = 0.0
        n = 0
        for _, mb in series:
            if prev is not None:
                if mb < prev:
                    used += prev - mb
                elif mb > prev:
                    added += mb - prev
            prev = mb
            n += 1
        return round(used, 2), round(added, 2), n

    # ------------------------------------------------------------------ days
    def daily(self, day: str) -> dict[str, Any]:
        start = datetime.fromisoformat(day).replace(tzinfo=CAIRO)
        end = start + timedelta(days=1)
        series = [r for r in self._readings(start.astimezone(timezone.utc)) if r[0] < end]
        before = self._before(start)
        used, added, n = self._drops(series, before[1] if before else None)
        last_mb = series[-1][1] if series else (before[1] if before else None)
        return {"used_mb": used, "added_mb": added, "changes": n, "last_mb": last_mb, "updated_at": iso()}

    # ------------------------------------------------------------------ sessions
    def add_session_event(self, on: bool, t_ms: int, device: str) -> str | None:
        """'started', 'ended', or None if ignored (duplicate, out of order, no open session)."""
        open_row = self.db.execute("SELECT start_ms FROM sessions WHERE end_ms IS NULL AND device = ? "
                                   "ORDER BY start_ms DESC LIMIT 1", (device,)).fetchone()
        if on:
            if open_row:  # already on: the phone restarted its service; keep the earlier start
                return None
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO sessions VALUES (?, NULL, ?)", (t_ms, device))
            self._pending[f"sessions/{t_ms}"] = self.session_doc(t_ms)
            return "started"
        if not open_row or t_ms < open_row[0]:
            event(log, logging.WARNING, "history.session_end_ignored", device=device,
                  reason="no open session" if not open_row else "ends before it started")
            return None
        with self.db:
            self.db.execute("UPDATE sessions SET end_ms = ? WHERE start_ms = ?", (t_ms, open_row[0]))
        self._pending[f"sessions/{open_row[0]}"] = self.session_doc(open_row[0])
        return "ended"

    def _session(self, start_ms: int) -> tuple[int, int | None] | None:
        return self.db.execute("SELECT start_ms, end_ms FROM sessions WHERE start_ms = ?", (start_ms,)).fetchone()

    def _cutoff(self, start_ms: int, end_ms: int | None) -> datetime:
        """Readings up to here count for the session: its end plus WE's lag, but not past the next start."""
        if end_ms is None:
            return utcnow()
        nxt = self.db.execute("SELECT MIN(start_ms) FROM sessions WHERE start_ms > ?", (start_ms,)).fetchone()[0]
        cut = end_ms + LAG_S * 1000
        return _from_ms(min(cut, nxt) if nxt else cut)

    def session_doc(self, start_ms: int) -> dict[str, Any]:
        s = self._session(start_ms)
        if not s:
            return {}
        start = _from_ms(s[0])
        series = [r for r in self._readings(start) if r[0] <= self._cutoff(*s)]
        before = self._before(start)
        used, _, _ = self._drops(series, before[1] if before else None)
        return {"start": iso(start), "end": iso(_from_ms(s[1])) if s[1] else None, "used_mb": used,
                "open": s[1] is None}

    def _sessions_touching(self, at: datetime) -> list[int]:
        """Sessions whose usage a reading at `at` can change: open ones, and ones within the lag."""
        rows = self.db.execute("SELECT start_ms, end_ms FROM sessions WHERE start_ms <= ? AND "
                               "(end_ms IS NULL OR end_ms >= ?)", (_ms(at), _ms(at) - LAG_S * 1000)).fetchall()
        return [r[0] for r in rows if at <= self._cutoff(*r) or r[1] is None]

    def sessions(self, since: datetime) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT start_ms FROM sessions WHERE start_ms >= ? ORDER BY start_ms",
                               (_ms(since),)).fetchall()
        return [{"start_ms": r[0], **self.session_doc(r[0])} for r in rows]

    # ------------------------------------------------------------------ Firebase mirror
    def take_pending(self) -> dict[str, Any]:
        """Paths under /usage to write (a multi-path PATCH). Give them back if the write fails."""
        out, self._pending = self._pending, {}
        return out

    def restore_pending(self, pending: dict[str, Any]) -> None:
        self._pending = {**pending, **self._pending}

    def queue_all(self, since: datetime | None = None) -> int:
        """Queue days, readings and sessions for upload: everything (first sync ever), or only
        what's newer than `since` (each restart, in case the last uploads didn't make it)."""
        days = set()
        for t, mb in self._readings(since):
            day = local_day(t)
            days.add(day)
            self._pending[f"readings/{day}/{t.astimezone(CAIRO):%H%M%S}"] = mb
        for day in days:
            self._pending[f"daily/{day}"] = self.daily(day)
        since_ms = _ms(since) if since else 0
        for (start_ms,) in self.db.execute("SELECT start_ms FROM sessions WHERE start_ms >= ? OR end_ms IS NULL",
                                           (since_ms,)).fetchall():
            self._pending[f"sessions/{start_ms}"] = self.session_doc(start_ms)
        return len(self._pending)

    # ------------------------------------------------------------------ backfill
    def backfill(self, logs_dir: Path) -> int:
        """Readings from the JSON event logs (scraper.jsonl and its rotations), oldest first."""
        found: list[tuple[datetime, float, float | None]] = []
        for f in sorted(logs_dir.glob("scraper.jsonl*")):
            try:
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    if '"quota.reading"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                        found.append((datetime.fromisoformat(d["ts"]), float(d["remaining_mb"]),
                                      d.get("total_mb")))
                    except (ValueError, KeyError, TypeError):
                        continue
            except OSError:
                continue
        stored = sum(self.add_reading(t, mb, total) for t, mb, total in sorted(found))
        self._pending.clear()  # the first cloud sync queues everything anyway
        event(log, logging.INFO, "history.backfilled", readings_seen=len(found), stored=stored)
        return stored
