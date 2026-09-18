"""Architecture invariants for the stop rebuild (#4060+).

Known defects are marked xfail(strict=True) until their owning Tasks land.
When the fix ships, remove the xfail — the test must then pass.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from stop.app import StopApp, WindowPane
from stop.host import FixtureHost, LiveHost
from stop.livelog import LiveLogFeed
from stop.metrics import METRICS, StopMetrics, calling_from_ui_thread
from stop.turn_stream import TurnStreamState

FIXTURE = Path(__file__).parent / "fixtures" / "basic"


class CountingLog:
    """Stand-in RichLog that records clear/write/scroll_end."""

    def __init__(self) -> None:
        self.ops: list[str] = []

    def clear(self) -> None:
        self.ops.append("CLEAR")

    def write(self, line: str, scroll_end: bool | None = None) -> None:
        self.ops.append(f"W:{scroll_end}")


def test_metrics_flush_has_no_secrets(tmp_path: Path):
    m = StopMetrics()
    m.record_snapshot(0.012)
    m.record_hardcopy("broca-athena", 0.05)
    m.note_turn_dropped()
    path = tmp_path / "metrics.jsonl"
    m.flush(path=path)
    text = path.read_text()
    assert "api_key" not in text.lower()
    assert "password" not in text.lower()
    assert "Bearer" not in text
    assert "snapshot_ms_last" in text


def test_idle_feed_records_zero_mutations():
    feed = LiveLogFeed(limit=50)
    log = CountingLog()
    assert feed.sync(log, "one\ntwo\n", source_key="a") == "replace"
    log.ops.clear()
    for _ in range(5):
        assert feed.sync(log, "one\ntwo\n", source_key="a") == "noop"
    assert log.ops == []


def test_calling_from_ui_thread_helper():
    ident = threading.get_ident()
    assert calling_from_ui_thread(ident) is True
    assert calling_from_ui_thread(ident + 1) is False
    assert calling_from_ui_thread(None) is False


def test_livehost_snapshot_must_not_run_on_ui_thread():
    """Invariant: LiveHost.snapshot must not execute on the Textual main thread.

    HostCollector clears ui_thread_ident before calling snapshot. Accidental
    UI-thread snapshot() still bumps METRICS.host_io_on_ui_thread.
    """
    host = LiveHost()
    host.ui_thread_ident = threading.get_ident()
    before = METRICS.host_io_on_ui_thread
    # Direct call from "UI" thread must be detected.
    try:
        host.snapshot()
    except Exception:
        pass
    assert METRICS.host_io_on_ui_thread > before

    # Collector path must not bump.
    before2 = METRICS.host_io_on_ui_thread
    from stop.collector import HostCollector

    col = HostCollector(host)
    col.start()
    try:
        deadline = time.time() + 3.0
        while time.time() < deadline and col.latest()[0] < 1:
            time.sleep(0.05)
        assert METRICS.host_io_on_ui_thread == before2
    finally:
        col.stop()


def test_turn_panel_must_not_change_win_log_geometry():
    """Invariant: showing/hiding the turn overlay must keep #win-log region stable."""

    async def run() -> None:
        app = StopApp(FixtureHost(FIXTURE))
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause(0.15)
            app.refresh_host()
            await pilot.pause(0.2)
            pane = app.query_one(WindowPane)
            win = app.query_one("#win-log")
            idle = win.region
            pane.show_turn(
                TurnStreamState(
                    enabled=True,
                    active=True,
                    agent_name="athena",
                    status="streaming",
                    text="> ask\n\n" + "\n".join(f"line {i}" for i in range(30)),
                    started_at=time.time(),
                )
            )
            await pilot.pause(0.15)
            shown = win.region
            pane.show_turn(TurnStreamState(enabled=True, active=False, status="idle"))
            await pilot.pause(0.15)
            hidden = win.region
            assert shown.height == idle.height and shown.width == idle.width, (
                f"turn show reflowed win-log: idle={idle} shown={shown} (#4064)"
            )
            assert hidden.height == idle.height and hidden.width == idle.width, (
                f"turn hide reflowed win-log: idle={idle} hidden={hidden} (#4064)"
            )

    asyncio.run(run())


def test_turn_panel_geometry_stable_at_breakpoints():
    """Same overlay invariant at medium and narrow widths."""

    async def check(size: tuple[int, int]) -> None:
        app = StopApp(FixtureHost(FIXTURE))
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.1)
            if size[0] < 80:
                app._narrow_page = "windows"
                app._apply_breakpoint()
            app.refresh_host()
            await pilot.pause(0.15)
            pane = app.query_one(WindowPane)
            win = app.query_one("#win-log")
            idle_h = win.region.height
            pane.show_turn(
                TurnStreamState(
                    enabled=True,
                    active=True,
                    agent_name="athena",
                    status="streaming",
                    text="line\n" * 20,
                    started_at=time.time(),
                )
            )
            await pilot.pause(0.1)
            assert win.region.height == idle_h
            pane.show_turn(TurnStreamState(enabled=True, active=False, status="idle"))
            await pilot.pause(0.1)
            assert win.region.height == idle_h

    asyncio.run(check((100, 30)))
    asyncio.run(check((60, 40)))


def test_fixture_multi_tick_idle_log_feed_is_noop():
    """After first paint, frozen fixture scrollback must not rewrite feeds."""
    host = FixtureHost(FIXTURE)
    feed = LiveLogFeed(limit=200)
    log = CountingLog()
    snap = host.snapshot()
    athena = next(a for a in snap.agents if a.name == "athena")
    w = next(x for x in athena.windows if (x.screen_name or "").startswith("broca-"))
    key = f"athena:{w.id}"
    feed.sync(log, w.last_scrollback or "", source_key=key)
    log.ops.clear()
    for _ in range(8):
        snap = host.snapshot()
        athena = next(a for a in snap.agents if a.name == "athena")
        w = next(x for x in athena.windows if (x.screen_name or "").startswith("broca-"))
        mode = feed.sync(log, w.last_scrollback or "", source_key=key)
        assert mode == "noop"
    assert log.ops == []


def test_selection_identity_fields_exist_on_window():
    """Baseline: Window.id is the stable selection key (#4067 will use it)."""
    host = FixtureHost(FIXTURE)
    snap = host.snapshot()
    ids = [w.id for a in snap.agents for w in a.windows]
    assert len(ids) == len(set(ids))
    assert any(i.endswith("/broca") for i in ids)


def test_no_sqlite_in_turn_stream_source():
    """#4069: stop must not open Broca production databases."""
    src = Path(__file__).resolve().parents[1] / "src" / "stop" / "turn_stream.py"
    text = src.read_text(encoding="utf-8")
    assert "import sqlite3" not in text
    assert "sqlite3.connect" not in text
