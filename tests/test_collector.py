"""HostCollector: off-UI serialized snapshots (#4061)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from stop.collector import HostCollector
from stop.host import FixtureHost, LiveHost
from stop.metrics import METRICS

FIXTURE = Path(__file__).parent / "fixtures" / "basic"


class SlowFixture(FixtureHost):
    """Fixture that sleeps inside snapshot to prove no overlap."""

    def __init__(self, root: Path, *, delay_s: float = 0.15):
        super().__init__(root)
        self.delay_s = delay_s
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = 0
        self._gate = threading.Lock()

    def snapshot(self):  # type: ignore[override]
        with self._gate:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.calls += 1
        try:
            time.sleep(self.delay_s)
            return super().snapshot()
        finally:
            with self._gate:
                self.in_flight -= 1


def test_collector_publishes_revision_and_snapshot():
    host = FixtureHost(FIXTURE)
    col = HostCollector(host)
    col.start()
    try:
        deadline = time.time() + 2.0
        rev, snap, err = 0, None, ""
        while time.time() < deadline:
            rev, snap, err = col.latest()
            if snap is not None:
                break
            time.sleep(0.02)
        assert snap is not None
        assert rev >= 1
        assert err == ""
        assert any(a.name == "athena" for a in snap.agents)
    finally:
        col.stop()


def test_collector_does_not_overlap_snapshots():
    host = SlowFixture(FIXTURE, delay_s=0.12)
    col = HostCollector(host)
    col.start()
    try:
        for _ in range(8):
            col.request()
            time.sleep(0.02)
        deadline = time.time() + 3.0
        while time.time() < deadline and host.calls < 2:
            time.sleep(0.05)
        # Give in-flight work time to finish.
        time.sleep(0.4)
        assert host.max_in_flight == 1, f"overlapped: max={host.max_in_flight}"
        assert host.calls >= 1
    finally:
        col.stop()


def test_collector_keeps_last_good_on_failure():
    host = FixtureHost(FIXTURE)
    col = HostCollector(host)
    col.start()
    try:
        deadline = time.time() + 2.0
        while col.latest()[1] is None and time.time() < deadline:
            time.sleep(0.02)
        rev1, snap1, _ = col.latest()
        assert snap1 is not None

        def boom():
            raise RuntimeError("inject failure")

        host.snapshot = boom  # type: ignore[method-assign]
        col.request()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            rev2, snap2, err = col.latest()
            if rev2 > rev1:
                assert snap2 is snap1  # same last-good object
                assert "inject failure" in err
                return
            time.sleep(0.02)
        raise AssertionError("collector never published failure revision")
    finally:
        col.stop()


def test_collector_snapshot_not_on_ui_thread_counter():
    """With ui_thread_ident set to 'this' thread, collector must clear it before snap."""
    host = LiveHost()
    # Simulate UI thread id so a mistaken snapshot() would bump the counter.
    ui = threading.get_ident()
    host.ui_thread_ident = ui
    before = METRICS.host_io_on_ui_thread
    col = HostCollector(host)
    col.start()
    try:
        deadline = time.time() + 3.0
        while time.time() < deadline:
            rev, snap, _ = col.latest()
            if rev >= 1:
                break
            time.sleep(0.05)
        # Worker clears ui_thread_ident before snapshot — counter must not rise.
        assert METRICS.host_io_on_ui_thread == before
    finally:
        col.stop()
