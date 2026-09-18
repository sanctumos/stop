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


def test_plan_reserves_round_robin_slots_for_unfocused_brocas():
    available = {
        "broca-selected",
        "broca-active",
        "letta",
        "broca-a",
        "broca-b",
        "broca-c",
        "broca-d",
    }
    focus = ["broca-selected", "broca-active", "letta"]
    first, cursor = LiveHost.plan_hardcopy_with_probes(
        focus,
        available=available,
        max_screens=5,
        probe_slots=2,
        probe_cursor=0,
    )
    second, _ = LiveHost.plan_hardcopy_with_probes(
        focus,
        available=available,
        max_screens=5,
        probe_slots=2,
        probe_cursor=cursor,
    )
    assert first[:3] == focus
    assert second[:3] == focus
    assert len(first) == len(second) == 5
    assert set(first[3:]) != set(second[3:])
    assert set(first + second) >= {
        "broca-selected",
        "broca-active",
        "letta",
        "broca-a",
        "broca-b",
        "broca-c",
        "broca-d",
    }
