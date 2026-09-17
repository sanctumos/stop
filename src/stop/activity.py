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

    def _key(w: Window) -> tuple:
        has_text = 1 if (w.last_scrollback or "").strip() else 0
        is_broca = 1 if (w.screen_name or "").startswith("broca-") else 0
        # Higher activity first; prefer real console text; prefer broca lanes.
        return (w.last_activity_epoch, has_text, is_broca)

    candidates.sort(key=_key, reverse=True)
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
