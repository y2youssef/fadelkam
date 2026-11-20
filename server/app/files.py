"""Writing files into state/: in one step, and readable by the owner only.

state/ holds the session cookie, the login ledger, readings and incident bundles. A half-written
ledger fails closed (hard stop), and a file other users can read leaks the session or the phone
number. So every file we write there goes through write_private.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: the lock isn't enforced there
    fcntl = None


class InstanceLock:
    """One process at a time may change state/: the server, a poller, a browser probe, or
    `probe reset`. Two browsers would share the profile, and two writers would each keep their own
    copy of the login ledger, so the min gap between logins wouldn't hold (FAILURE_MODES #46, #47).
    flock: the OS drops the lock when the process dies, so a crash never leaves it stuck."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self._f = None

    def acquire(self) -> None:
        """Raises SystemExit (with the holder's pid) if another process holds it."""
        if fcntl is None or self._f:
            return
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        f = open(self.state_dir / "instance.lock", "a+", encoding="utf-8")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.seek(0)
            holder = f.read().strip() or "?"
            f.close()
            raise SystemExit(f"another server/poller/probe (pid {holder}) is using {self.state_dir}. "
                             "Stop it first (systemctl --user stop we-quota).") from None
        f.truncate(0)
        f.write(f"{os.getpid()}\n")
        f.flush()
        self._f = f

    def release(self) -> None:
        if self._f:
            self._f.close()
            self._f = None


def write_private(path: Path, text: str) -> None:
    """Replace `path` with `text` atomically, mode 0600; creates the folder (0700) if missing."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())  # a power cut must not leave an empty ledger behind the rename
    try:
        os.chmod(tmp, 0o600)  # os.open's mode doesn't apply to a tmp file left by a crash
    except OSError:
        pass
    os.replace(tmp, path)
