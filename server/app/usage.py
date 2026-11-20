"""Usage from the local history (state/usage.db), from server/:

    python -m app.usage                 # per day, last 7 days
    python -m app.usage days 30         # per day, last 30 days
    python -m app.usage sessions 2      # WE mobile data sessions of the last 2 days

Or query the file directly: sqlite3 state/usage.db "SELECT * FROM daily"
Read-only and offline: safe to run while the service is up.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from .config import load_settings
from .history import CAIRO, History, local_day
from .logs import utcnow


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.usage")
    ap.add_argument("what", nargs="?", choices=("days", "sessions"), default="days")
    ap.add_argument("n", nargs="?", type=int, default=None, help="how many days back")
    args = ap.parse_args(argv)
    h = History(load_settings().state_dir / "usage.db")
    if args.what == "days":
        n = args.n or 7
        today = utcnow().astimezone(CAIRO).date()
        print(f"{'day':<12}{'used MB':>10}{'added MB':>10}{'left MB':>10}{'changes':>9}")
        for i in range(n - 1, -1, -1):
            day = (today - timedelta(days=i)).isoformat()
            d = h.daily(day)
            left = f"{d['last_mb']:.0f}" if d["last_mb"] is not None else "-"
            print(f"{day:<12}{d['used_mb']:>10.1f}{d['added_mb']:>10.1f}{left:>10}{d['changes']:>9}")
    else:
        n = args.n or 2
        rows = h.sessions(utcnow() - timedelta(days=n))
        if not rows:
            print("No sessions yet (the phone reports WE mobile data on/off).")
        local = lambda iso_t: datetime.fromisoformat(iso_t).astimezone(CAIRO)  # noqa: E731
        for s in rows:
            start = local(s["start"])
            end = "now (data on)" if s.get("open") else f"{local(s['end']):%H:%M}"
            print(f"{local_day(start)}  {start:%H:%M} -> {end:<14} {s['used_mb']:>8.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
