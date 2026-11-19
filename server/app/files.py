"""Writing files into state/: in one step, and readable by the owner only.

state/ holds the session cookie, the login ledger, readings and incident bundles. A half-written
ledger fails closed (hard stop), and a file other users can read leaks the session or the phone
number. So every file we write there goes through write_private.
"""
from __future__ import annotations

import os
from pathlib import Path


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
