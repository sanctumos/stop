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


@pytest.mark.xfail(
    strict=True,
    reason="Host I/O still runs on Textual UI thread — fix #4061",
)
def test_livehost_snapshot_must_not_run_on_ui_thread():
    """Invariant: LiveHost.snapshot must not execute on the Textual main thread."""
    host = LiveHost()
    host.ui_thread_ident = threading.get_ident()
    before = METRICS.host_io_on_ui_thread
    # Even without real screens, snapshot path should detect UI-thread violation.
    try:
        host.snapshot()
    except Exception:
        pass
    assert METRICS.host_io_on_ui_thread == before, (
        "snapshot ran on the UI thread (counter bumped) — move collection off-loop (#4061)"
    )


@pytest.mark.xfail(
    strict=True,
    reason="dock:bottom still reflows #win-log — fix #4064",
)
def test_turn_panel_must_not_change_win_log_geometry():
    """Invariant: showing/hiding the turn overlay must keep #win-log region stable."""

    async def run() -> None:
        app = StopApp(FixtureHost(FIXTURE))
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause(0.1)
            app.refresh_host()
            await pilot.pause(0.1)
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
            assert shown == idle, (
                f"turn show reflowed win-log: idle={idle} shown={shown} (#4064)"
            )
            assert hidden == idle, (
                f"turn hide reflowed win-log: idle={idle} hidden={hidden} (#4064)"
            )

    asyncio.run(run())


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
