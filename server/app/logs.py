"""Structured logging + secret redaction.

* `state/logs/scraper.jsonl` – every event, one JSON object per line (rotated 5 MB x 10).
* `state/logs/api.jsonl`     – one line per portal API call (endpoint, status, retCode, errorNo, ms).
* console                    – short human-readable lines.

Everything that leaves the process (log lines, incident files) passes through REDACT, which
scrubs registered secrets (password, phone number, session tokens) wherever they appear.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Redactor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, str] = {}

    def add(self, value: str | None, label: str = "secret", keep_last: int = 0) -> None:
        if not value or len(value) < 4:
            return
        tail = value[-keep_last:] if keep_last else ""
        repl = f"<{label}{'…' + tail if tail else ''}>"
        variants = {value}
        if label == "phone" and value.startswith("0"):
            variants |= {value[1:], "20" + value[1:], "+20" + value[1:]}
        with self._lock:
            for v in variants:
                self._subs[v] = repl

    def __call__(self, text: str) -> str:
        if not text:
            return text
        with self._lock:
            items = sorted(self._subs.items(), key=lambda kv: -len(kv[0]))
        for secret, repl in items:
            if secret in text:
                text = text.replace(secret, repl)
        return text


REDACT = Redactor()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="seconds")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d: dict[str, Any] = {
            "ts": iso(datetime.fromtimestamp(record.created, timezone.utc)),
            "lvl": record.levelname,
            "log": record.name,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            d.update(fields)
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return REDACT(json.dumps(d, ensure_ascii=False, default=str))


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        fields = getattr(record, "fields", None) or {}
        kv = " ".join(f"{k}={v}" for k, v in fields.items())
        line = f"{ts} {record.levelname:<7} {record.getMessage()} {kv}".rstrip()
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return REDACT(line)


_configured = False


def setup_logging(logs_dir: Path, level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    logs_dir.mkdir(parents=True, exist_ok=True)

    def rotating(name: str) -> logging.Handler:
        h = logging.handlers.RotatingFileHandler(logs_dir / name, maxBytes=5_000_000, backupCount=10, encoding="utf-8")
        h.setFormatter(_JsonFormatter())
        return h

    root = logging.getLogger("we")
    root.setLevel(level)
    root.propagate = False
    main_file = rotating("scraper.jsonl")
    root.addHandler(main_file)
    console = logging.StreamHandler()
    console.setFormatter(_ConsoleFormatter())
    root.addHandler(console)

    # uvicorn (startup, errors, access log) goes to the same file + console, also redacted.
    uv = logging.getLogger("uvicorn")
    uv.setLevel(logging.INFO)
    uv.propagate = False
    uv.addHandler(main_file)
    uv.addHandler(console)

    # Every API call goes only to api.jsonl (high volume); failures are re-logged on "we.portal".
    api = logging.getLogger("we.api")
    api.propagate = False
    api.setLevel(logging.INFO)
    api.addHandler(rotating("api.jsonl"))
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"we.{name}")


def event(logger: logging.Logger, level: int, name: str, exc_info: bool = False, **fields: Any) -> None:
    logger.log(level, name, extra={"fields": fields}, exc_info=exc_info)
