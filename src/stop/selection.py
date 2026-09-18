"""Selection by stable identity — indexes are display-only (#4067)."""

from __future__ import annotations

from .models import Agent, Window


def resolve_agent_selection(
    agents: list[Agent],
    *,
    selected_name: str | None,
) -> tuple[int, str | None, bool]:
    """Map ``selected_name`` onto ``agents``.

    Returns ``(index, resolved_name, fell_back)``. If the name is missing,
    fall back once to index 0 (or empty). Does not oscillate — caller keeps
    the new name after fallback.
    """
    if not agents:
        return 0, None, bool(selected_name)
    if selected_name:
        for i, a in enumerate(agents):
            if a.name == selected_name:
                return i, selected_name, False
        # Disappeared — nearest documented fallback: first visible row.
        return 0, agents[0].name, True
    return 0, agents[0].name, False


def resolve_window_selection(
    windows: list[Window],
    *,
    selected_window_id: str | None,
) -> tuple[int, str | None, bool]:
    """Map ``selected_window_id`` onto ``windows`` (same contract as agents)."""
    if not windows:
        return 0, None, bool(selected_window_id)
    if selected_window_id:
        for i, w in enumerate(windows):
            if w.id == selected_window_id:
                return i, selected_window_id, False
        return 0, windows[0].id, True
    return 0, windows[0].id, False
