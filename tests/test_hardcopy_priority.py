"""Deterministic hardcopy priority (#4062)."""

from __future__ import annotations

from stop.host import LiveHost


def test_prioritize_keeps_selected_active_letta_when_capped():
    focus = [
        "broca-athena",
        "broca-ada",
        "letta",
        "broca-rico",
        "broca-monday",
        "broca-wren",
    ]
    available = set(focus) | {"broca-extra"}
    got = LiveHost.prioritize_hardcopy(focus, available=available, max_screens=4)
    assert got == ["broca-athena", "broca-ada", "letta", "broca-rico"]
    assert "broca-athena" in got and "letta" in got and "broca-ada" in got


def test_prioritize_is_stable_across_runs():
    focus = ["broca-z", "broca-a", "letta", "broca-m"]
    available = set(focus)
    a = LiveHost.prioritize_hardcopy(focus, available=available, max_screens=4)
    b = LiveHost.prioritize_hardcopy(focus, available=available, max_screens=4)
    assert a == b == ["broca-z", "broca-a", "letta", "broca-m"]


def test_prioritize_skips_missing_and_never_hardcopy():
    focus = ["stop", "broca-athena", "missing", "letta"]
    available = {"broca-athena", "letta", "broca-other"}
    got = LiveHost.prioritize_hardcopy(focus, available=available, max_screens=4)
    assert got == ["broca-athena", "letta"]


def test_set_focus_screens_dedupes_preserving_order():
    h = LiveHost.__new__(LiveHost)
    h._focus_order = []
    h.set_focus_screens(["broca-a", "broca-a", "broca-b", "letta"])
    assert h._focus_order == ["broca-a", "broca-b", "letta"]
    h.set_focus_screens(["broca-x"])
    assert h._focus_order[0] == "broca-x"
    assert h._focus_order[-1] == "letta"
