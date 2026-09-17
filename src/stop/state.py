"""Window state machine: running / dead / missing / unmanaged / returned."""

from __future__ import annotations

from .models import ScreenSession, WindowState


def classify_window(
    *,
    cron_managed: bool,
    screen: ScreenSession | None,
    previous: WindowState | None = None,
) -> WindowState:
    """Classify a window from current screen presence + cron membership.

    Rules (Doc #1376 §5):
    - unmanaged: no cron line → never red, even if no screen
    - running: Detached/Attached with live session
    - dead: screen -list says Dead
    - missing: cron-managed, no session
    - returned: was dead/missing, now running again (caller may flash)
    """
    if not cron_managed:
        if screen is not None and screen.status != "Dead":
            # Unmanaged but somehow running — still show as unmanaged grey? Spec says
            # unmanaged = start script, no cron. If it's running anyway, report running.
            return WindowState.RUNNING
        return WindowState.UNMANAGED

    if screen is None:
        new = WindowState.MISSING
    elif screen.status == "Dead":
        new = WindowState.DEAD
    else:
        new = WindowState.RUNNING

    if (
        previous in (WindowState.DEAD, WindowState.MISSING)
        and new == WindowState.RUNNING
    ):
        return WindowState.RETURNED
    return new


def is_failure(state: WindowState) -> bool:
    return state in (WindowState.DEAD, WindowState.MISSING)


def badge_label(state: WindowState) -> str:
    return {
        WindowState.RUNNING: "running",
        WindowState.DEAD: "DEAD",
        WindowState.MISSING: "missing",
        WindowState.UNMANAGED: "unmanaged",
        WindowState.RETURNED: "returned",
    }[state]
