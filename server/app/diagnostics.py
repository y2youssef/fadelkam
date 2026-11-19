"""Diagnostics: API call recorder, portal-version tracking and incident bundles.

When something breaks we want to answer "what did WE actually send back?" without
re-running anything. So:
  * every portal API response is summarised to state/logs/api.jsonl and kept (with body) in a
    ring buffer;
  * every failure dumps an incident folder: meta.json, screenshot.png, page.html, api_calls.json;
  * the portal's frontend build id (main.<hash>.chunk.js) is tracked, and a change is logged
    loudly, because a new build is the most likely reason selectors/flows suddenly fail.
Auth-related request bodies are never stored; auth response bodies are reduced to their shape.
"""
from __future__ import annotations

import json
import logging
import shutil
import traceback
from collections import deque
from pathlib import Path
from typing import Any

from .config import Settings
from .files import write_private
from .logs import REDACT, event, get_logger, iso, utcnow

log = get_logger("portal")
api_log = get_logger("api")

AUTH_HINTS = ("/auth/", "SmsVerification", "VerifyCode", "validateToken", "Password", "password", "logOut")
MAX_BODY_CHARS = 200_000


def shape(obj: Any, key: str | None = None) -> Any:
    """Structure of a JSON value with scalar values replaced by type tags (keeps 'header' intact)."""
    if key == "header":
        return obj
    if isinstance(obj, dict):
        return {k: shape(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shape(obj[0])] + ([f"... {len(obj) - 1} more"] if len(obj) > 1 else []) if obj else []
    if isinstance(obj, str):
        return f"<str:{len(obj)}>"
    if isinstance(obj, bool) or obj is None:
        return obj
    return f"<{type(obj).__name__}>"


class ApiRecorder:
    def __init__(self, settings: Settings, maxlen: int = 300) -> None:
        self.s = settings
        self.calls: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.portal_build: str | None = None
        self.device_id: str | None = None
        self._version_file = settings.state_dir / "portal_version.json"

    def attach(self, context) -> None:
        context.on("response", self._on_response)

    async def _on_response(self, resp) -> None:
        try:
            url = resp.url
            if "/static/js/main." in url:
                self._note_build(url.rsplit("/", 1)[-1])
                return
            if "/busiservice/" not in url:
                return
            endpoint = url.split("/busiservice/", 1)[1].split("?", 1)[0]
            req = resp.request
            tok = req.headers.get("csrftoken")
            if tok:
                REDACT.add(tok, "csrftoken")
            dev = req.headers.get("deviceid")
            if dev and dev != self.device_id:
                if self.device_id:
                    event(log, logging.WARNING, "portal.deviceid_changed", old=self.device_id[:12], new=dev[:12])
                self.device_id = dev
            try:
                body = await resp.json()
            except Exception:
                body = None
            header = body.get("header") if isinstance(body, dict) and isinstance(body.get("header"), dict) else {}
            t = req.timing
            ms = round(t["responseEnd"]) if t and t.get("responseEnd", -1) >= 0 else None
            is_auth = any(h in endpoint for h in AUTH_HINTS)
            req_body = None if is_auth else req.post_data
            entry = {
                "ts": iso(), "method": req.method, "endpoint": endpoint, "status": resp.status,
                "ret_code": header.get("retCode"), "error_no": header.get("errorNo"),
                "error_msg": header.get("errorMsg"), "ms": ms,
                "req_body": "<omitted:auth>" if is_auth else (req_body[:5000] if req_body else None),
                "body": shape(body) if is_auth else body,
            }
            self.calls.append(entry)
            summary = {k: entry[k] for k in ("method", "endpoint", "status", "ret_code", "error_no", "error_msg", "ms")}
            api_log.info("api", extra={"fields": summary})
            if resp.status >= 400 or entry["ret_code"] not in (None, "0"):
                event(log, logging.WARNING, "api.not_ok", **summary)
            if "logOutForUser" in endpoint:
                # We never log out on purpose, so this is the app ending our session.
                event(log, logging.WARNING, "portal.app_logged_out", page_url=resp.frame.url if resp.frame else None)
        except Exception:
            event(log, logging.DEBUG, "recorder.error", exc_info=True)

    def last(self, endpoint_part: str) -> dict[str, Any] | None:
        for c in reversed(self.calls):
            if endpoint_part in c["endpoint"]:
                return c
        return None

    def _note_build(self, name: str) -> None:
        if name == self.portal_build:
            return
        self.portal_build = name
        prev = None
        try:
            prev = json.loads(self._version_file.read_text(encoding="utf-8")).get("main")
        except (OSError, ValueError):
            pass
        if prev != name:
            if prev:
                event(log, logging.WARNING, "portal.frontend_changed", old=prev, new=name,
                      hint="WE deployed a new web build; if login/quota now fails, re-run the probe")
            write_private(self._version_file, json.dumps({"main": name, "first_seen": iso(), "previous": prev}))


def _write(path: Path, text: str) -> None:
    write_private(path, REDACT(text))


async def save_incident(settings: Settings, kind: str, *, page=None, recorder: ApiRecorder | None = None,
                        error: BaseException | None = None, extra: dict | None = None) -> Path:
    """Dump everything useful for a post-mortem. Never raises."""
    d = settings.incidents_dir / f"{utcnow():%Y%m%dT%H%M%SZ}_{kind}"
    try:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)  # screenshots show the phone number
        meta: dict[str, Any] = {"kind": kind, "ts": iso(), "extra": extra or {}}
        if error is not None:
            meta["error"] = {"type": type(error).__name__, "message": str(error),
                             **({k: v for k, v in error.as_dict().items() if k != "incident"}
                                if hasattr(error, "as_dict") else {}),
                             "traceback": "".join(traceback.format_exception(type(error), error, error.__traceback__))}
        if recorder is not None:
            meta["portal_build"] = recorder.portal_build
            calls = []
            for c in recorder.calls:
                if len(json.dumps(c.get("body"), default=str)) > MAX_BODY_CHARS:
                    c = {**c, "body": "<truncated>"}
                calls.append(c)
            _write(d / "api_calls.json", json.dumps(calls, indent=1, ensure_ascii=False, default=str))
        if page is not None:
            meta["url"] = page.url
            try:
                await page.screenshot(path=str(d / "screenshot.png"), full_page=True, timeout=15_000)
            except Exception as e:
                meta["screenshot_error"] = str(e)
            try:
                # React mirrors controlled input values into the DOM, so the HTML can contain the
                # typed password in clear text -> always redacted.
                _write(d / "page.html", await page.content())
            except Exception as e:
                meta["html_error"] = str(e)
        _write(d / "meta.json", json.dumps(meta, indent=1, ensure_ascii=False, default=str))
        event(log, logging.WARNING, "incident.saved", kind=kind, path=str(d))
    except Exception:
        event(log, logging.ERROR, "incident.save_failed", kind=kind, exc_info=True)
    _prune(settings)
    return d


def _prune(settings: Settings) -> None:
    try:
        dirs = sorted(p for p in settings.incidents_dir.iterdir() if p.is_dir())
        for p in dirs[: max(0, len(dirs) - settings.incident_retention)]:
            shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass
