"""Identity-stable selection across inventory churn (#4067)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from textual.widgets import RichLog

from stop.app import AgentList, StopApp, WindowPane
from stop.host import FixtureHost
from stop.models import Agent, Window, WindowState
from stop.selection import resolve_agent_selection, resolve_window_selection

FIXTURE = Path(__file__).parent / "fixtures" / "basic"


def _agent(name: str, *win_ids: str) -> Agent:
    wins = [
        Window(
            id=wid,
            label=wid,
            screen_name=f"broca-{name}" if i == 0 else wid,
            state=WindowState.RUNNING,
            last_seen_pid=1000 + i,
            last_scrollback=f"log-{wid}\n",
        )
        for i, wid in enumerate(win_ids)
    ]
    return Agent(name=name, cron_managed=True, windows=wins)


def test_resolve_agent_keeps_name_across_reorder():
    agents = [_agent("aaa", "a1"), _agent("bbb", "b1"), _agent("ccc", "c1")]
    idx, name, fell = resolve_agent_selection(agents, selected_name="bbb")
    assert (idx, name, fell) == (1, "bbb", False)
    reordered = [_agent("ccc", "c1"), _agent("bbb", "b1"), _agent("aaa", "a1")]
    idx, name, fell = resolve_agent_selection(reordered, selected_name="bbb")
    assert (idx, name, fell) == (1, "bbb", False)


def test_resolve_agent_insert_before_keeps_identity():
    base = [_agent("bbb", "b1"), _agent("ccc", "c1")]
    idx, name, fell = resolve_agent_selection(base, selected_name="bbb")
    assert name == "bbb" and fell is False
    grown = [_agent("aaa", "a1"), _agent("bbb", "b1"), _agent("ccc", "c1")]
    idx, name, fell = resolve_agent_selection(grown, selected_name="bbb")
    assert (idx, name, fell) == (1, "bbb", False)


def test_resolve_agent_fallback_once():
    agents = [_agent("aaa", "a1"), _agent("ccc", "c1")]
    idx, name, fell = resolve_agent_selection(agents, selected_name="bbb")
    assert fell is True
    assert name == "aaa"
    assert idx == 0


def test_resolve_window_survives_secondary_churn():
    wins = [
        Window(id="broca-x", label="broca", state=WindowState.RUNNING),
        Window(id="run-x", label="run", state=WindowState.RUNNING),
    ]
    idx, wid, fell = resolve_window_selection(wins, selected_window_id="run-x")
    assert (idx, wid, fell) == (1, "run-x", False)
    # Broca restart with new pid — id stable.
    wins2 = [
        Window(
            id="broca-x",
            label="broca",
            state=WindowState.RUNNING,
            last_seen_pid=9999,
        ),
        Window(id="run-x", label="run", state=WindowState.RUNNING),
        Window(id="cron-x", label="cron", state=WindowState.RUNNING),
    ]
    idx, wid, fell = resolve_window_selection(wins2, selected_window_id="run-x")
    assert (idx, wid, fell) == (1, "run-x", False)


def test_resolve_window_fallback_when_removed():
    wins = [Window(id="broca-x", label="broca", state=WindowState.RUNNING)]
    idx, wid, fell = resolve_window_selection(wins, selected_window_id="gone")
    assert fell is True and wid == "broca-x"


def test_app_keeps_agent_across_reorder_and_expand():
    clears: list[str] = []
    orig_clear = RichLog.clear

    def tracking_clear(self):  # noqa: ANN001
        clears.append(self.id or "?")
        return orig_clear(self)

    async def run() -> None:
        app = StopApp(FixtureHost(FIXTURE))
        with patch.object(RichLog, "clear", tracking_clear):
            async with app.run_test(size=(160, 45)) as pilot:
                for _ in range(40):
                    if app._snap is not None:
                        break
                    app.refresh_host()
                    await pilot.pause(0.05)
                agents_w = app.query_one(AgentList)
                # Pick a mid agent if present; else first.
                vis = agents_w.visible()
                assert vis
                target = vis[min(1, len(vis) - 1)].name
                agents_w.selected_name = target
                agents_w.set_agents(list(reversed(vis)) + vis)
                assert agents_w.selected_name == target
                assert agents_w.selected() is not None
                assert agents_w.selected().name == target

                # Expand/collapse must not clear win-log for same window.
                pane = app.query_one(WindowPane)
                agent = agents_w.selected()
                assert agent and agent.windows
                app.selected_window_id = agent.windows[0].id
                pane.show_agent(agent, window_id=app.selected_window_id)
                await pilot.pause(0.05)
                clears.clear()
                pane.expanded = True
                pane.show_agent(agent, window_id=app.selected_window_id)
                pane.expanded = False
                pane.show_agent(agent, window_id=app.selected_window_id)
                await pilot.pause(0.05)
                assert "win-log" not in clears

    asyncio.run(run())
