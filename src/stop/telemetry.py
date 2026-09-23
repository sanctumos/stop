"""Invisible, self-eroding trace for transient TUI failures.

Not a log. One JSON file, rewritten in place, capped by age, count, and
bytes. Nothing is printed, and nothing is drawn. Otto reads the file after
a miss; the file then forgets the miss on its own.

Default path: ``/tmp/stop-<uid>/trace.json`` (same directory as the
append-only counters in ``metrics.py``, which this does not touch).
``STOP_TRACE=0`` disables recording.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from .metrics import metrics_dir

MAX_EVENTS = 180
MAX_AGE_S = 2 * 60 * 60
MAX_BYTES = 192 * 1024
FIELD_MAX = 240
COALESCE_S = 8.0
FLUSH_INTERVAL_S = 1.0

# These are the rows worth flushing immediately. Everything else can wait
# a second so a tight poll loop does not rewrite the file on every tick.
IMMEDIATE = frozenset(
    {
        "seek_start",
        "seek_give_up",
        "stream_error",
        "stream_end",
    }
)

_SECRET_KEY = re.compile(
    r"(api[_-]?key|token|password|secret|authorization|cookie)",
    re.I,
)
# Bearer tokens / long hex-ish secrets embedded in free text.
_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._\-/+=]{12,}"
    r"|sk-[a-z0-9]{16,}"
    r"|[a-f0-9]{32,}"
    r"|[A-Za-z0-9_-]{40,})"
)


def trace_path(uid: int | None = None) -> Path:
    return metrics_dir(uid) / "trace.json"


def _scrub_string(value: str) -> str:
    s = value if len(value) <= FIELD_MAX else value[: FIELD_MAX - 1] + "…"
    return _SECRET_VALUE.sub("[redacted]", s)


def _redact(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "…"
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:24]:
            name = str(key)[:80]
            if _SECRET_KEY.search(name):
                continue
            out[name] = _redact(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth=depth + 1) for item in list(value)[:12]]
    return _scrub_string(str(value)[:FIELD_MAX])


def _signature(component: str, event: str, detail: dict[str, Any]) -> str:
    return json.dumps(
        [component, event, detail],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


class TraceRing:
    """Bounded event list. Oldest and expired rows are dropped on write."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        max_events: int = MAX_EVENTS,
        max_age_s: float = MAX_AGE_S,
        max_bytes: int = MAX_BYTES,
        coalesce_s: float = COALESCE_S,
        load: bool = True,
    ) -> None:
        self.path = path if path is not None else trace_path()
        self.max_events = max_events
        self.max_age_s = max_age_s
        self.max_bytes = max_bytes
        self.coalesce_s = coalesce_s
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._last_flush = 0.0
        if load and self.path.is_file():
            self._load()

    def event(self, component: str, name: str, **detail: Any) -> None:
        if os.environ.get("STOP_TRACE", "1") == "0":
            return
        clean = _redact(detail)
        if not isinstance(clean, dict):
            clean = {"detail": clean}
        now = time.time()
        sig = _signature(component, name, clean)
        with self._lock:
            self._erode(now)
            if self._events:
                last = self._events[-1]
                if (
                    last.get("_sig") == sig
                    and now - float(last.get("ts") or 0) <= self.coalesce_s
                ):
                    last["n"] = int(last.get("n") or 1) + 1
                    last["ts"] = round(now, 3)
                    self._flush(now, force=name in IMMEDIATE)
                    return
            self._events.append(
                {
                    "ts": round(now, 3),
                    "component": str(component)[:40],
                    "event": str(name)[:40],
                    "n": 1,
                    "detail": clean,
                    "_sig": sig,
                }
            )
            self._erode(now)
            self._flush(now, force=name in IMMEDIATE)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_public(row) for row in self._events]

    def _erode(self, now: float) -> None:
        cutoff = now - self.max_age_s
        self._events = [
            row for row in self._events if float(row.get("ts") or 0) >= cutoff
        ]
        if len(self._events) > self.max_events:
            self._events = self._events[-self.max_events :]
        while self._events and _encoded_size(self._events) > self.max_bytes:
            self._events.pop(0)

    def _flush(self, now: float, *, force: bool) -> None:
        if not force and now - self._last_flush < FLUSH_INTERVAL_S:
            return
        payload = {
            "v": 1,
            "events": [_public(row) for row in self._events],
        }
        raw = json.dumps(payload, separators=(",", ":"), default=str)
        try:
            self.path.parent.mkdir(mode=0o700, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(raw, encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            self._last_flush = now
        except OSError:
            return

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        rows = data.get("events") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return
        kept: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
            component = str(row.get("component") or "")
            name = str(row.get("event") or "")
            item = {
                "ts": float(row.get("ts") or 0),
                "component": component[:40],
                "event": name[:40],
                "n": int(row.get("n") or 1),
                "detail": detail,
                "_sig": _signature(component, name, detail),
            }
            kept.append(item)
        self._events = kept
        self._erode(time.time())


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": row.get("ts"),
        "component": row.get("component"),
        "event": row.get("event"),
        "n": row.get("n") or 1,
        "detail": row.get("detail") or {},
    }


def _encoded_size(events: list[dict[str, Any]]) -> int:
    payload = {"v": 1, "events": [_public(row) for row in events]}
    return len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))


TRACE = TraceRing(load=True)


def trace(component: str, event: str, **detail: Any) -> None:
    """Record one diagnostic event. Safe to call from worker threads."""
    try:
        TRACE.event(component, event, **detail)
    except Exception:
        return
