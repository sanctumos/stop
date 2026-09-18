"""Multi-tick Textual idle: log panes must not mutate after first paint (#4063)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from textual.widgets import RichLog

from stop.app import StopApp
from stop.host import FixtureHost

FIXTURE = Path(__file__).parent / "fixtures" / "basic"


def test_fixture_app_idle_ticks_do_not_clear_or_rewrite_logs():
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
                # Wait for collector first paint.
                for _ in range(40):
                    if app._snap is not None:
                        break
                    app.refresh_host()
                    await pilot.pause(0.05)
                assert app._snap is not None
                await pilot.pause(0.1)
                clears.clear()
                writes.clear()
                for _ in range(6):
                    app.refresh_host()
                    await pilot.pause(0.05)
                # Frozen fixture: no new scrollback → no clear/write on log panes.
                log_clears = [c for c in clears if c.endswith("-log") or c == "win-log"]
                log_writes = [w for w in writes if w.endswith("-log") or w == "win-log"]
                assert log_clears == [], f"unexpected clears: {log_clears}"
                assert log_writes == [], f"unexpected writes: {log_writes}"

    asyncio.run(run())
