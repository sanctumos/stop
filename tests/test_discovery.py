from pathlib import Path

from stop.activity import pick_active_now, rank_active_windows
from stop.host import FixtureHost
from stop.models import WindowState
from stop.parsers import parse_crontab, parse_screen_list
from stop.state import classify_window

FIXTURE = Path(__file__).parent / "fixtures" / "basic"


def test_parse_crontab_agents():
    text = (FIXTURE / "crontab.txt").read_text()
    managed = parse_crontab(text)
    assert "athena" in managed
    assert "ada" in managed
    assert "rico" in managed
    assert "bramwell" not in managed
    assert "__letta__" in managed
    assert "__smcp__" in managed
    assert managed["athena"].endswith("start-athena-broca.sh")


def test_parse_screen_list():
    text = (FIXTURE / "screen-list.txt").read_text()
    sessions = parse_screen_list(text)
    by = {s.name: s for s in sessions}
    assert by["broca-athena"].pid == 1818
    assert by["broca-athena"].status == "Detached"
    assert by["stop"].status == "Detached"
    assert by["letta"].pid == 1201


def test_parse_screen_dead():
    text = (FIXTURE / "screen-list.1.txt").read_text()
    by = {s.name: s for s in parse_screen_list(text)}
    assert by["broca-rico"].status == "Dead"


def test_state_unmanaged_never_red():
    assert classify_window(cron_managed=False, screen=None) == WindowState.UNMANAGED


def test_state_missing_and_dead():
    from stop.models import ScreenSession

    assert (
        classify_window(cron_managed=True, screen=None) == WindowState.MISSING
    )
    dead = ScreenSession("broca-rico", 1, "Dead")
    assert classify_window(cron_managed=True, screen=dead) == WindowState.DEAD


def test_state_returned():
    from stop.models import ScreenSession

    live = ScreenSession("broca-rico", 9999, "Detached")
    assert (
        classify_window(
            cron_managed=True, screen=live, previous=WindowState.MISSING
        )
        == WindowState.RETURNED
    )
    assert (
        classify_window(
            cron_managed=True, screen=live, previous=WindowState.DEAD
        )
        == WindowState.RETURNED
    )


def test_fixture_snapshot_inventory():
    (FIXTURE / "tick").write_text("0")
    host = FixtureHost(FIXTURE)
    snap = host.snapshot()
    names = {a.name for a in snap.agents}
    assert {"athena", "ada", "rico", "bramwell", "System"} <= names
    assert "stop" not in names  # excluded as agent
    bram = next(a for a in snap.agents if a.name == "bramwell")
    assert bram.cron_managed is False
    assert bram.state == WindowState.UNMANAGED
    athena = next(a for a in snap.agents if a.name == "athena")
    assert athena.state == WindowState.RUNNING


def test_crash_return_timeline_and_events():
    (FIXTURE / "tick").write_text("0")
    host = FixtureHost(FIXTURE)
    s0 = host.snapshot()
    rico0 = next(a for a in s0.agents if a.name == "rico")
    assert rico0.state == WindowState.RUNNING

    host.bump_tick()  # → Dead
    s1 = host.snapshot()
    rico1 = next(a for a in s1.agents if a.name == "rico")
    assert rico1.state == WindowState.DEAD
    assert any("died" in e.message for e in s1.events)

    host.bump_tick()  # → missing
    s2 = host.snapshot()
    rico2 = next(a for a in s2.agents if a.name == "rico")
    assert rico2.state == WindowState.MISSING

    host.bump_tick()  # → returned
    s3 = host.snapshot()
    rico3 = next(a for a in s3.agents if a.name == "rico")
    assert rico3.state == WindowState.RETURNED
    assert rico3.windows[0].last_seen_pid == 9999
    assert any("back" in e.message for e in s3.events)


def test_active_now_excludes_letta_and_prefers_hottest():
    host = FixtureHost(FIXTURE)
    snap = host.snapshot()
    ranked = rank_active_windows(snap.agents)
    names = [w.screen_name for w in ranked]
    assert "letta" not in names
    assert "stop" not in names
    active = pick_active_now(snap.agents)
    assert active is not None
    assert active.screen_name != "letta"
    # ada scrollback was touched last in fixture setup
    assert active.screen_name == "broca-ada"


def test_follow_lock():
    host = FixtureHost(FIXTURE)
    snap = host.snapshot()
    athena_win = next(
        w
        for a in snap.agents
        if a.name == "athena"
        for w in a.windows
        if w.screen_name == "broca-athena"
    )
    locked = pick_active_now(snap.agents, follow_lock_id=athena_win.id)
    assert locked is not None
    assert locked.id == athena_win.id
