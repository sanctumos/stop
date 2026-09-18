"""Letta pane gets scrollback; Active Now still excludes letta."""

from pathlib import Path

from stop.host import FixtureHost
from stop.activity import pick_active_now, rank_active_windows

FIXTURE = Path(__file__).parent / "fixtures" / "basic"


def test_fixture_loads_letta_scrollback_on_system_window():
    host = FixtureHost(FIXTURE)
    snap = host.snapshot()
    system = next(a for a in snap.agents if a.name == "System")
    letta = next(w for w in system.windows if w.screen_name == "letta")
    assert letta.last_scrollback.strip()
    # Still never an Active Now target.
    assert "letta" not in [w.screen_name for w in rank_active_windows(snap.agents)]
    active = pick_active_now(snap.agents)
    assert active is None or active.screen_name != "letta"
