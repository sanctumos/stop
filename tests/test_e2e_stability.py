"""End-to-end Textual stability suites required by #4072."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from textual.widgets import RichLog

from stop.app import StopApp, WindowPane
from stop.host import FixtureHost, SCROLLBACK_MAX_BYTES
from stop.turn_stream import (
    TurnStreamState,
    TurnStreamWorker,
    prune_seen_runs,
)

FIXTURE = Path(__file__).parent / "fixtures" / "basic"
SIZES = ((160, 45), (100, 30), (60, 40), (60, 20))


async def _wait_snap(app: StopApp, pilot, *, ticks: int = 40) -> None:
    for _ in range(ticks):
        if app._snap is not None:
            return
        app.refresh_host()
        await pilot.pause(0.05)
    assert app._snap is not None


def test_turn_lifecycle_geometry_and_scroll_at_breakpoints():
    """hidden → seeking → streaming → linger → hidden keeps #win-log stable."""

    async def check(size: tuple[int, int]) -> None:
        app = StopApp(FixtureHost(FIXTURE))
        async with app.run_test(size=size) as pilot:
            await _wait_snap(app, pilot)
            if size[0] < 80:
                app._narrow_page = "windows"
                app._apply_breakpoint()
            app.refresh_host()
            await pilot.pause(0.1)
            pane = app.query_one(WindowPane)
            win = app.query_one("#win-log", RichLog)
            idle_region = win.region
            scroll_y = win.scroll_y

            phases = [
                TurnStreamState(
                    enabled=True,
                    active=True,
                    agent_name="athena",
                    status="seeking",
                    text="",
                    started_at=time.time(),
                ),
                TurnStreamState(
                    enabled=True,
                    active=True,
                    agent_name="athena",
                    status="streaming",
                    text="> ask\n\n" + "\n".join(f"line {i}" for i in range(25)),
                    started_at=time.time(),
                ),
                TurnStreamState(
                    enabled=True,
                    active=True,
                    agent_name="athena",
                    status="linger",
                    lingering=True,
                    text="final answer\n",
                    started_at=time.time(),
                    linger_until=time.time() + 60,
                ),
                TurnStreamState(enabled=True, active=False, status="idle"),
            ]
            for st in phases:
                pane.show_turn(st)
                await pilot.pause(0.08)
                assert win.region.width == idle_region.width
                assert win.region.height == idle_region.height
                # Base log scroll must not jump solely because overlay painted.
                assert abs(win.scroll_y - scroll_y) < 2.0

    for size in SIZES:
        asyncio.run(check(size))


def test_collector_failure_does_not_block_input_path():
    """Overlapping/slow collect must not freeze action handlers (#4072 §3)."""

    class SlowHost(FixtureHost):
        def __init__(self, root: Path):
            super().__init__(root)
            self.gate = threading.Event()
            self.entered = threading.Event()

        def snapshot(self):  # type: ignore[override]
            self.entered.set()
            self.gate.wait(timeout=2.0)
            return super().snapshot()

    host = SlowHost(FIXTURE)
    app = StopApp(host)

    async def run() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            app.refresh_host()
            # Wait until background snapshot is blocked.
            deadline = time.time() + 2.0
            while time.time() < deadline and not host.entered.is_set():
                await pilot.pause(0.02)
            assert host.entered.is_set()
            # UI actions still run while collect is stuck.
            t0 = time.time()
            app.action_focus_active()
            app.action_cycle()
            assert time.time() - t0 < 0.5
            host.gate.set()
            await pilot.pause(0.2)

    asyncio.run(run())


def test_resource_bounds_seen_runs_events_scrollback():
    from stop.host import _tail_text
    from stop.models import CrashEvent

    order = [f"run-{i}" for i in range(250)]
    kept, s = prune_seen_runs(order, maxlen=100)
    assert len(kept) == 100
    assert len(s) == 100
    assert kept[0] == "run-150"

    w = TurnStreamWorker(agents_root=Path("/tmp"))
    assert getattr(w, "SEEN_RUNS_MAX", 100) <= 200

    host = FixtureHost(FIXTURE)
    host._events = [CrashEvent(time.time(), f"e{i}") for i in range(80)]
    snap = host.snapshot()
    assert len(snap.events) <= 50

    big = "x" * (SCROLLBACK_MAX_BYTES * 3)
    assert len(_tail_text(big).encode("utf-8", errors="replace")) <= SCROLLBACK_MAX_BYTES + 64


def test_idle_multi_tick_no_geometry_or_feed_churn():
    async def run() -> None:
        app = StopApp(FixtureHost(FIXTURE))
        async with app.run_test(size=(160, 45)) as pilot:
            await _wait_snap(app, pilot)
            win = app.query_one("#win-log", RichLog)
            region0 = win.region
            feed = app.query_one(WindowPane)._feed
            key0 = feed._source_key
            lines0 = list(feed._lines)
            for _ in range(8):
                app.refresh_host()
                await pilot.pause(0.05)
            assert win.region == region0
            assert feed._source_key == key0
            assert feed._lines == lines0

    asyncio.run(run())


def test_follow_lock_and_help_filter_tiny_height():
    async def run() -> None:
        app = StopApp(FixtureHost(FIXTURE))
        async with app.run_test(size=(60, 20)) as pilot:
            await _wait_snap(app, pilot)
            assert "tiny" in app.screen.classes or "narrow" in app.screen.classes
            await pilot.press("question_mark")
            await pilot.pause(0.05)
            await pilot.press("escape")
            await pilot.pause(0.05)
            await pilot.press("slash")
            await pilot.pause(0.05)
            await pilot.press("escape")
            await pilot.pause(0.05)
            await pilot.press("p")
            await pilot.pause(0.05)
            # Active Now pin toggles without crashing on tiny height
            app.refresh_host()
            await pilot.pause(0.05)

    asyncio.run(run())
