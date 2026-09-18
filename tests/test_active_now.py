"""Active Now dialogue-vs-noise classification."""

from __future__ import annotations

import os
import time
from pathlib import Path

from stop.activity import (
    KIND_DIALOGUE,
    KIND_NOISE,
    classify_line,
    classify_scrollback,
    latest_meaningful_log_epoch,
    next_activity_epoch,
    pick_active_now,
    rank_active_windows,
    scrollback_delta_is_noise_only,
)
from stop.host import FixtureHost
from stop.models import Agent, Window, WindowState

FIXTURE = Path(__file__).parent / "fixtures" / "basic"


def test_bridge_write_is_noise():
    assert (
        classify_line(
            "plugin: Wrote Otto bridge response file: /home/rizzn/.../otto_bridge/outbox/x.json"
        )
        == KIND_NOISE
    )
    assert classify_line("Retrieved 0 messages from web chat API") == KIND_NOISE
    assert (
        classify_line(
            "plugins.rico_kitchen_webchat.api_client: Retrieved 0 partner-bridge messages"
        )
        == KIND_NOISE
    )
    assert classify_line("Retrieved 0 inbox messages") == KIND_NOISE
    assert classify_line("Retrieved 2 partner-bridge messages") != KIND_NOISE
    assert (
        classify_line(
            'httpx: HTTP Request: POST http://localhost:8284/v1/agents/x/messages "HTTP/1.1 200 OK"'
        )
        != KIND_NOISE
    )
    assert (
        classify_line("httpx: HTTP Request: GET http://127.0.0.1:8284/v1/health")
        == KIND_NOISE
    )
    assert (
        classify_line(
            "HTTP Request: PATCH http://localhost:8284/v1/agents/x HTTP/1.1 200 OK"
        )
        == KIND_NOISE
    )


def test_telegram_inbound_is_dialogue():
    assert classify_line("ada busy telegram inbound") == KIND_DIALOGUE
    assert classify_line("received a message from user") == KIND_DIALOGUE


def test_noise_delta_does_not_count_as_activity():
    old = "hello\n"
    new = old + "plugin: Wrote Otto bridge response file: /tmp/x\n"
    assert scrollback_delta_is_noise_only(old, new) is True
    new2 = old + "telegram inbound from Mark\n"
    assert scrollback_delta_is_noise_only(old, new2) is False


def test_meaningful_activity_epoch_persists_and_ignores_newer_http_noise():
    old = (
        "[2026-09-18 12:38:28] INFO telegram inbound from Mark\n"
        "[2026-09-18 12:38:29] INFO HTTP Request: PATCH "
        "http://localhost:8284/v1/agents/x HTTP/1.1 200 OK\n"
    )
    epoch = latest_meaningful_log_epoch(old)
    assert epoch > 0
    assert next_activity_epoch(0.0, "", old, now=epoch + 100) == epoch
    assert next_activity_epoch(epoch, old, old, now=epoch + 200) == epoch

    noise = old + (
        "[2026-09-18 12:40:00] INFO HTTP Request: PATCH "
        "http://localhost:8284/v1/agents/x HTTP/1.1 200 OK\n"
    )
    assert next_activity_epoch(epoch, old, noise, now=epoch + 300) == epoch


def test_active_now_prefers_dialogue_over_newer_bridge_noise():
    """Newer bridge-only Broca must lose to older dialogue Broca."""
    now = time.time()
    dialogue = Window(
        id="ada/broca",
        label="broca-ada",
        screen_name="broca-ada",
        state=WindowState.RUNNING,
        last_activity_epoch=now - 60,
        last_scrollback="ada busy telegram inbound\nada busy again\n",
    )
    noise = Window(
        id="rico/broca",
        label="broca-rico",
        screen_name="broca-rico",
        state=WindowState.RUNNING,
        last_activity_epoch=now,  # newer
        last_scrollback=(
            "plugin: Wrote Otto bridge response file: "
            "/home/rizzn/sanctum/agents/rico/broca/run/otto_bridge/outbox/x.json\n"
            "httpx: HTTP Request: PATCH http://localhost:8284/v1/agents/x\n"
        ),
    )
    agents = [
        Agent(name="ada", cron_managed=True, windows=[dialogue]),
        Agent(name="rico", cron_managed=True, windows=[noise]),
    ]
    ranked = rank_active_windows(agents)
    assert ranked[0].screen_name == "broca-ada"
    active = pick_active_now(agents)
    assert active is not None
    assert active.screen_name == "broca-ada"
    kind, hits, _ = classify_scrollback(active.last_scrollback)
    assert kind == KIND_DIALOGUE
    assert hits >= 1


def test_all_noise_still_shows_quiet_window():
    """Noise-only hosts show the hottest console labeled quiet — not an empty pane."""
    now = time.time()
    noise = Window(
        id="ada/broca",
        label="broca-ada",
        screen_name="broca-ada",
        state=WindowState.RUNNING,
        last_activity_epoch=now,
        last_scrollback="Wrote Otto bridge response file: /tmp/x\n",
    )
    agents = [Agent(name="ada", cron_managed=True, windows=[noise])]
    active = pick_active_now(agents)
    assert active is not None
    assert active.screen_name == "broca-ada"
    kind, _, _ = classify_scrollback(active.last_scrollback)
    assert kind == KIND_NOISE  # UI renders this as "quiet"


def test_newer_one_line_beats_older_many_dialogue_hits():
    """Epoch wins over stale hit-count (#4068)."""
    now = time.time()
    old_busy = Window(
        id="zzz/broca",
        label="broca-old",
        screen_name="broca-old",
        state=WindowState.RUNNING,
        last_activity_epoch=now - 120,
        last_scrollback="\n".join(
            [f"telegram inbound msg {i}" for i in range(10)]
        )
        + "\n",
    )
    fresh = Window(
        id="aaa/broca",
        label="broca-new",
        screen_name="broca-new",
        state=WindowState.RUNNING,
        last_activity_epoch=now,
        last_scrollback="telegram inbound just now\n",
    )
    agents = [
        Agent(name="old", cron_managed=True, windows=[old_busy]),
        Agent(name="new", cron_managed=True, windows=[fresh]),
    ]
    ranked = rank_active_windows(agents)
    assert ranked[0].id == "aaa/broca"
    assert pick_active_now(agents).id == "aaa/broca"


def test_wrapped_inbound_record_stays_dialogue_and_beats_newer_signal():
    now = time.time()
    ada_dialogue = Window(
        id="ada/broca",
        label="broca-ada",
        screen_name="broca-ada",
        state=WindowState.RUNNING,
        last_activity_epoch=now - 60,
        last_scrollback=(
            "[2026-09-18 12:49:41] INFO "
            "plugins.telegram_bot.message_handler: Coalesced\n"
            "inbound queued message_id=841 parts=1\n"
        ),
    )
    porter_error = Window(
        id="porter/broca",
        label="broca-porter",
        screen_name="broca-porter",
        state=WindowState.RUNNING,
        last_activity_epoch=now,
        last_scrollback="Error polling partner-bridge inbox: DNS failure\n",
    )
    agents = [
        Agent(name="ada", cron_managed=True, windows=[ada_dialogue]),
        Agent(name="porter", cron_managed=True, windows=[porter_error]),
    ]
    kind, _, _ = classify_scrollback(ada_dialogue.last_scrollback)
    assert kind == KIND_DIALOGUE
    assert pick_active_now(agents).id == "ada/broca"


def test_equal_epoch_tie_breaks_by_stable_id():
    now = time.time()
    a = Window(
        id="bbb/broca",
        label="b",
        screen_name="broca-b",
        state=WindowState.RUNNING,
        last_activity_epoch=now,
        last_scrollback="telegram inbound\n",
    )
    b = Window(
        id="aaa/broca",
        label="a",
        screen_name="broca-a",
        state=WindowState.RUNNING,
        last_activity_epoch=now,
        last_scrollback="telegram inbound\n",
    )
    agents = [
        Agent(name="b", cron_managed=True, windows=[a]),
        Agent(name="a", cron_managed=True, windows=[b]),
    ]
    first = rank_active_windows(agents)[0].id
    for _ in range(20):
        assert rank_active_windows(agents)[0].id == first
    assert first == "aaa/broca"  # lexicographically smaller id wins on full tie


def test_follow_lock_released_when_missing():
    from stop.activity import follow_lock_still_present

    now = time.time()
    w = Window(
        id="ada/broca",
        label="broca-ada",
        screen_name="broca-ada",
        state=WindowState.RUNNING,
        last_activity_epoch=now,
        last_scrollback="telegram inbound\n",
    )
    agents = [Agent(name="ada", cron_managed=True, windows=[w])]
    assert follow_lock_still_present(agents, "ada/broca") is True
    assert follow_lock_still_present(agents, "gone") is False
    locked = pick_active_now(agents, follow_lock_id="ada/broca")
    assert locked is not None and locked.id == "ada/broca"


def test_fixture_ada_still_wins_with_dialogue_text():
    (FIXTURE / "tick").write_text("0")
    now = time.time()
    os.utime(FIXTURE / "scrollback" / "broca-athena.txt", (now - 30, now - 30))
    os.utime(FIXTURE / "scrollback" / "broca-rico.txt", (now - 20, now - 20))
    os.utime(FIXTURE / "scrollback" / "broca-ada.txt", (now - 1, now - 1))
    # Make rico "newer" bridge noise file content via scrollback rewrite in memory after snap —
    # fixture ada text already has "telegram inbound".
    host = FixtureHost(FIXTURE)
    snap = host.snapshot()
    active = pick_active_now(snap.agents)
    assert active is not None
    assert active.screen_name == "broca-ada"

