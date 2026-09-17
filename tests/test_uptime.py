"""Agent-list uptime is screen start — not chatty log mtime."""

from __future__ import annotations

import time
from pathlib import Path

from stop.activity import scrollback_delta_is_noise_only
from stop.app import _age
from stop.parsers import parse_screen_list, parse_screen_started


def test_age_formats_days():
    now = time.time()
    assert _age(now - 45, now) == "45s"
    assert _age(now - 600, now) == "10m"
    assert _age(now - 7200, now) == "2h"
    assert _age(now - 86400, now) == "1d"
    assert _age(now - (86400 + 5 * 3600), now) == "1d5h"
    assert _age(now - 10 * 86400, now).startswith("10d")


def test_parse_screen_started_am_pm():
    epoch = parse_screen_started("09/16/2026 01:19:41 PM")
    assert epoch > 0
    # Round-trip through screen-list line shape.
    text = (
        "There are screens on:\n"
        "\t1018140.broca-longfellow\t(09/16/2026 01:19:41 PM)\t(Detached)\n"
        "\t1818.broca-athena\t(09/07/2026 06:32:01 PM)\t(Detached)\n"
    )
    by = {s.name: s for s in parse_screen_list(text)}
    assert by["broca-longfellow"].started_at_epoch == epoch
    assert by["broca-athena"].started_at_epoch < by["broca-longfellow"].started_at_epoch


def test_webchat_poll_noise_does_not_count_as_activity_delta():
    old = "[INFO] plugins.q_vernal_webchat.api_client: Retrieved 0 messages from web chat API\n"
    new = (
        old
        + "[INFO] plugins.q_vernal_webchat.api_client: Retrieved 0 messages from web chat API\n"
    )
    assert scrollback_delta_is_noise_only(old, new) is True
