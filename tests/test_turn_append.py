"""Turn pane: append-only + no clear thrash on incremental updates (#4065)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from textual.widgets import RichLog

from stop.app import StopApp, WindowPane
from stop.host import FixtureHost
from stop.turn_stream import TurnStreamState

FIXTURE = Path(__file__).parent / "fixtures" / "basic"


def test_turn_pane_appends_without_clear_after_first_paint():
    clears: list[str] = []
    writes: list[str] = []

    orig_clear = RichLog.clear
    orig_write = RichLog.write

    def tracking_clear(self):  # noqa: ANN001
        clears.append(self.id or "?")
        return orig_clear(self)

    def tracking_write(self, *a, **k):  # noqa: ANN001
        writes.append(self.id or "?")
        return orig_write(self, *a, **k)

    async def run() -> None:
        app = StopApp(FixtureHost(FIXTURE))
        with patch.object(RichLog, "clear", tracking_clear), patch.object(
            RichLog, "write", tracking_write
        ):
            async with app.run_test(size=(160, 45)) as pilot:
                pane = app.query_one(WindowPane)
                base = TurnStreamState(
                    enabled=True,
                    active=True,
                    agent_name="athena",
                    run_id="run-abc",
                    status="streaming",
                    text="line1\nline2",
                    started_at=1_700_000_000.0,
                )
                pane.show_turn(base)
                # Flush reveal paint (call_after_refresh) before measuring idle ticks.
                for _ in range(10):
                    await pilot.pause(0.05)
                    if "turn-log" in writes:
                        break
                assert "turn-log" in writes, "first turn paint never wrote"
                clears.clear()
                writes.clear()

                # Hundreds of identical ticks — must not clear or rewrite.
                for _ in range(200):
                    pane.show_turn(base)
                await pilot.pause(0.05)
                assert clears == [], f"unexpected clears on identical: {clears}"
                assert writes == [], f"unexpected writes on identical: {writes}"

                # Incremental append — clear forbidden; write only new line(s).
                longer = TurnStreamState(
                    enabled=True,
                    active=True,
                    agent_name="athena",
                    run_id="run-abc",
                    status="streaming",
                    text="line1\nline2\nline3",
                    started_at=1_700_000_000.0,
                )
                pane.show_turn(longer)
                await pilot.pause(0.02)
                turn_clears = [c for c in clears if c == "turn-log"]
                turn_writes = [w for w in writes if w == "turn-log"]
                assert turn_clears == [], f"clear after append: {turn_clears}"
                assert turn_writes, "expected at least one append write"
                assert len(turn_writes) <= 3

                # New run id → replace (clear allowed once).
                clears.clear()
                writes.clear()
                other = TurnStreamState(
                    enabled=True,
                    active=True,
                    agent_name="athena",
                    run_id="run-xyz",
                    status="streaming",
                    text="fresh",
                    started_at=1_700_000_000.0,
                )
                pane.show_turn(other)
                await pilot.pause(0.02)
                assert "turn-log" in clears

    asyncio.run(run())


def test_live_log_feed_noop_on_identical_source():
    from textual.app import App
    from textual.widgets import RichLog

    from stop.livelog import LiveLogFeed

    class T(App):
        def compose(self):
            yield RichLog(id="l")

    async def run() -> None:
        async with T().run_test() as pilot:
            log = pilot.app.query_one(RichLog)
            feed = LiveLogFeed(limit=50, width=100)
            assert feed.sync(log, "a\nb", source_key="r1") == "replace"
            assert feed.sync(log, "a\nb", source_key="r1") == "noop"
            assert feed.sync(log, "a\nb\nc", source_key="r1") == "append"
            assert feed.sync(log, "a\nb\nc", source_key="r1") == "noop"

    asyncio.run(run())
