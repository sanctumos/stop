"""Active Now ranking — latest non-excluded activity."""

from __future__ import annotations

from .models import EXCLUDED_SCREEN_NAMES, Agent, Window, WindowState


def rank_active_windows(
    agents: list[Agent],
    *,
    exclude_screens: frozenset[str] | None = None,
) -> list[Window]:
    """Windows sorted by last_activity_epoch descending, excluding Letta/stop/etc."""
    exclude = exclude_screens if exclude_screens is not None else EXCLUDED_SCREEN_NAMES
    candidates: list[Window] = []
    for agent in agents:
        for w in agent.windows:
            if w.screen_name and w.screen_name in exclude:
                continue
            if w.state == WindowState.UNMANAGED and w.last_activity_epoch <= 0:
                continue
            candidates.append(w)
    candidates.sort(key=lambda w: w.last_activity_epoch, reverse=True)
    return candidates


def pick_active_now(
    agents: list[Agent],
    *,
    follow_lock_id: str | None = None,
    exclude_screens: frozenset[str] | None = None,
) -> Window | None:
    """Return the follow-locked window if set, else the hottest eligible window."""
    ranked = rank_active_windows(agents, exclude_screens=exclude_screens)
    if follow_lock_id:
        for w in ranked:
            if w.id == follow_lock_id:
                return w
        # Lock target gone — fall through to auto.
    return ranked[0] if ranked else None
