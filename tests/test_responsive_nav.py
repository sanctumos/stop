"""Responsive navigation, follow-tail, and page chrome (#4070)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from textual.widgets import Input, Static

from stop.app import StopApp, WindowPane
from stop.host import FixtureHost
from stop.livelog import LiveLogFeed
from stop.turn_stream import TurnStreamState

FIXTURE = Path(__file__).parent / "fixtures" / "basic"
SIZES = ((160, 45), (100, 30), (60, 40), (60, 20))


def _chrome_text(app: StopApp) -> str:
    chrome = app.query_one("#chrome", Static)
    return str(getattr(chrome, "_paint_body", None) or chrome.content or "")


async def _wait_snap(app: StopApp, pilot, *, ticks: int = 40) -> None:
    for _ in range(ticks):
        if app._snap is not None:
            return
        app.refresh_host()
        await pilot.pause(0.05)
    assert app._snap is not None


def test_feed_follow_false_passes_scroll_end_false():
    """Manual scrollback: appends must not force scroll_end=True."""
    calls: list[bool | None] = []

    class FakeLog:
        def clear(self) -> None:
            pass

        def write(self, line: str, scroll_end: bool | None = None) -> None:
            calls.append(scroll_end)

    feed = LiveLogFeed(limit=50)
    log = FakeLog()
    feed.sync(log, "one\ntwo\n", source_key="a", follow=True)
    calls.clear()
    feed.sync(log, "one\ntwo\nthree\n", source_key="a", follow=False)
    assert calls == [False]


def test_chrome_and_bottom_stack_at_all_breakpoints():
    async def check(size: tuple[int, int]) -> None:
        app = StopApp(FixtureHost(FIXTURE))
        async with app.run_test(size=size) as pilot:
            await _wait_snap(app, pilot)
            app._update_chrome()
            await pilot.pause(0.05)
            text = _chrome_text(app)
            assert text.strip() != ""
            events = app.query_one("#events")
            filt = app.query_one("#filter", Input)
            footer = app.query_one("Footer")
            assert "visible" not in filt.classes
            assert events.display
            assert footer.display
            # Bottom chrome sits above the footer (margin reserves footer row).
            bottom = app.query_one("#bottom-chrome")
            assert bottom.region.y + bottom.region.height <= footer.region.y
            assert events.region.y + events.region.height <= footer.region.y
            if size[0] < 80:
                assert "narrow" in app.screen.classes
                assert "page:agents" in text
            elif size[0] < 120:
                assert "medium" in app.screen.classes
            else:
                assert "layout:wide" in text

    for size in SIZES:
        asyncio.run(check(size))


def test_narrow_page_keys_and_turn_reveal():
    async def run() -> None:
        app = StopApp(FixtureHost(FIXTURE))
        async with app.run_test(size=(60, 40)) as pilot:
            await _wait_snap(app, pilot)
            assert "narrow" in app.screen.classes
            assert app._narrow_page == "agents"
            await pilot.press("tab")
            await pilot.pause(0.05)
            assert app._narrow_page == "windows", _chrome_text(app)
            assert "page-windows" in app.screen.classes
            await pilot.press("a")
            await pilot.pause(0.05)
            assert app._narrow_page == "active"
            await pilot.press("l")
            await pilot.pause(0.05)
            assert app._narrow_page == "letta"

            # Simulate turn start on agents page → auto windows reveal.
            app._narrow_page = "agents"
            app._apply_breakpoint()
            app._turn_was_active = False
            worker = MagicMock()
            worker.snapshot.return_value = TurnStreamState(
                enabled=True,
                active=True,
                agent_name="athena",
                status="streaming",
                text="hi",
                started_at=1.0,
            )
            app._turn = worker
            app._maybe_narrow_turn_reveal()
            app._update_chrome()
            assert app._narrow_page == "windows"
            assert "page-windows" in app.screen.classes
            assert "turn:athena/streaming" in _chrome_text(app)

    asyncio.run(run())


def test_pageup_holds_follow_tail_end_resumes():
    async def run() -> None:
        app = StopApp(FixtureHost(FIXTURE))
        async with app.run_test(size=(160, 45)) as pilot:
            await _wait_snap(app, pilot)
            pane = app.query_one(WindowPane)
            pane.focus()
            await pilot.pause(0.05)
            assert pane.follow_tail is True
            await pilot.press("pageup")
            await pilot.pause(0.05)
            assert pane.follow_tail is False
            assert "scroll:held" in _chrome_text(app)
            await pilot.press("end")
            await pilot.pause(0.05)
            assert pane.follow_tail is True

    asyncio.run(run())


def test_filter_visible_does_not_cover_events():
    async def run() -> None:
        app = StopApp(FixtureHost(FIXTURE))
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait_snap(app, pilot)
            await pilot.press("slash")
            await pilot.pause(0.05)
            filt = app.query_one("#filter", Input)
            events = app.query_one("#events")
            assert "visible" in filt.classes
            assert filt.region.y >= events.region.y
            assert events.region.height >= 1
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert "visible" not in filt.classes

    asyncio.run(run())


def test_key_matrix_help_lists_scroll_bindings():
    async def run() -> None:
        app = StopApp(FixtureHost(FIXTURE))
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.press("question_mark")
            await pilot.pause(0.05)
            body = app.screen.query_one("#help-body", Static)
            text = str(body.content)
            assert "PgUp/PgDn" in text
            assert "Home/End" in text
            await pilot.press("escape")

    asyncio.run(run())
