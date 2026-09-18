"""Lightweight stop metrics — no secrets, no message bodies."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def metrics_dir(uid: int | None = None) -> Path:
    return Path(f"/tmp/stop-{uid if uid is not None else os.getuid()}")


@dataclass
class StopMetrics:
    """Process-local counters flushed as JSONL under /tmp/stop-<uid>/."""

    snapshot_count: int = 0
    snapshot_ms_total: float = 0.0
    snapshot_ms_last: float = 0.0
    hardcopy_ms: dict[str, float] = field(default_factory=dict)
    render_revision: int = 0
    turn_updates_dropped: int = 0
    turn_updates_coalesced: int = 0
    turn_updates_applied: int = 0
    refresh_errors: int = 0
    host_io_on_ui_thread: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_snapshot(self, duration_s: float) -> None:
        with self._lock:
            self.snapshot_count += 1
            ms = duration_s * 1000.0
            self.snapshot_ms_total += ms
            self.snapshot_ms_last = ms

    def record_hardcopy(self, screen_name: str, duration_s: float) -> None:
        with self._lock:
            self.hardcopy_ms[screen_name] = duration_s * 1000.0

    def bump_render_revision(self) -> int:
        with self._lock:
            self.render_revision += 1
            return self.render_revision

    def note_turn_dropped(self) -> None:
        with self._lock:
            self.turn_updates_dropped += 1

    def note_turn_coalesced(self) -> None:
        with self._lock:
            self.turn_updates_coalesced += 1

    def note_turn_applied(self) -> None:
        with self._lock:
            self.turn_updates_applied += 1

    def note_refresh_error(self) -> None:
        with self._lock:
            self.refresh_errors += 1

    def note_host_io_on_ui_thread(self) -> None:
        with self._lock:
            self.host_io_on_ui_thread += 1

    def snapshot_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ts": time.time(),
                "snapshot_count": self.snapshot_count,
                "snapshot_ms_total": round(self.snapshot_ms_total, 3),
                "snapshot_ms_last": round(self.snapshot_ms_last, 3),
                "hardcopy_ms": dict(self.hardcopy_ms),
                "render_revision": self.render_revision,
                "turn_updates_dropped": self.turn_updates_dropped,
                "turn_updates_coalesced": self.turn_updates_coalesced,
                "turn_updates_applied": self.turn_updates_applied,
                "refresh_errors": self.refresh_errors,
                "host_io_on_ui_thread": self.host_io_on_ui_thread,
            }

    def flush(self, *, path: Path | None = None) -> Path:
        d = metrics_dir()
        d.mkdir(mode=0o700, exist_ok=True)
        out = path or (d / "metrics.jsonl")
        line = json.dumps(self.snapshot_dict(), separators=(",", ":"))
        with out.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return out


# Shared process singleton (UI + host collect into the same counters).
METRICS = StopMetrics()


def calling_from_ui_thread(ui_ident: int | None) -> bool:
    """True when *ui_ident* is the Textual main thread and matches current."""
    if ui_ident is None:
        return False
    return threading.get_ident() == ui_ident
