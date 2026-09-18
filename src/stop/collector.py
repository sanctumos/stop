"""Background host collection — keep Textual off subprocess/hardcopy I/O."""

from __future__ import annotations

import threading
import time
from typing import Callable

from .host import HostBackend, LiveHost
from .metrics import METRICS
from .models import HostSnapshot


class HostCollector:
    """One serialized worker that owns `host.snapshot()` and publishes revisions.

    The UI thread must only call `request()`, `latest()`, and `stop()`.
    """

    def __init__(
        self,
        host: HostBackend,
        *,
        on_update: Callable[[], None] | None = None,
    ) -> None:
        self.host = host
        self.on_update = on_update
        self._lock = threading.Lock()
        self._latest: HostSnapshot | None = None
        self._revision = 0
        self._error = ""
        self._busy = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        # Bumped when UI wants a collect; worker clears after starting one.
        self._pending = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="stop-host-collector", daemon=True
        )
        self._thread.start()
        # Kick an immediate first snapshot.
        self.request()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None

    def request(self) -> None:
        """Ask for a snapshot. No-ops if one is already running (no overlap)."""
        with self._lock:
            self._pending = True
        self._wake.set()

    def latest(self) -> tuple[int, HostSnapshot | None, str]:
        """Return (revision, snapshot_or_none, error). Snapshot is last good."""
        with self._lock:
            return self._revision, self._latest, self._error

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            if self._stop.is_set():
                break
            with self._lock:
                if not self._pending or self._busy:
                    continue
                self._pending = False
                self._busy = True
            try:
                # Never mark LiveHost UI-thread for this worker.
                if isinstance(self.host, LiveHost):
                    self.host.ui_thread_ident = None
                snap = self.host.snapshot()
                with self._lock:
                    self._latest = snap
                    self._revision += 1
                    self._error = ""
                if self.on_update:
                    try:
                        self.on_update()
                    except Exception:
                        pass
            except Exception as exc:  # noqa: BLE001
                METRICS.note_refresh_error()
                with self._lock:
                    # Keep previous _latest; surface error once.
                    self._error = str(exc)[:200]
                    self._revision += 1  # so UI notices the error state
                if self.on_update:
                    try:
                        self.on_update()
                    except Exception:
                        pass
            finally:
                with self._lock:
                    self._busy = False
                    # If another request arrived while busy, wake again.
                    if self._pending and not self._stop.is_set():
                        self._wake.set()
