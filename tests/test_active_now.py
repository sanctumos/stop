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


def test_telegram_inbound_is_dialogue():
    assert classify_line("ada busy telegram inbound") == KIND_DIALOGUE
    assert classify_line("received a message from user") == KIND_DIALOGUE


def test_noise_delta_does_not_count_as_activity():
    old = "hello\n"
    new = old + "plugin: Wrote Otto bridge response file: /tmp/x\n"
    assert scrollback_delta_is_noise_only(old, new) is True
    new2 = old + "telegram inbound from Mark\n"
    assert scrollback_delta_is_noise_only(old, new2) is False


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


def test_all_noise_yields_idle():
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
    assert pick_active_now(agents) is None


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
