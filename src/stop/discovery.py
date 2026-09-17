"""Discover agents + windows from agents dir, crontab, and screen list."""

from __future__ import annotations

from pathlib import Path

from .models import (
    EXCLUDED_SCREEN_NAMES,
    Agent,
    ScreenSession,
    Window,
    WindowState,
)
from .parsers import discover_agent_dirs, find_start_script, parse_crontab
from .state import classify_window


def _screen_by_name(screens: list[ScreenSession]) -> dict[str, ScreenSession]:
    return {s.name: s for s in screens}


def build_agents(
    *,
    agents_root: Path,
    crontab_text: str,
    screens: list[ScreenSession],
    previous: dict[str, WindowState] | None = None,
    logs_root: Path | None = None,
    now_epoch: float = 0.0,
) -> list[Agent]:
    """Compose Agent list for one tick.

    previous: map of window_id → prior WindowState (for RETURNED detection).
    """
    previous = previous or {}
    managed = parse_crontab(crontab_text)
    dirs = discover_agent_dirs(agents_root)
    by_screen = _screen_by_name(screens)

    # Union of cron agents + on-disk agents.
    names = sorted(set(dirs) | {k for k in managed if not k.startswith("__")})

    agents: list[Agent] = []
    for name in names:
        agent_dir = dirs.get(name)
        cron_managed = name in managed
        start = None
        if agent_dir is not None:
            start = find_start_script(agent_dir)
        start_path = str(start) if start else managed.get(name) or None

        screen_name = f"broca-{name}"
        # Never invent a broca window for the stop session name collision.
        screen = by_screen.get(screen_name)
        win_id = f"{name}/broca"
        prev = previous.get(win_id)
        state = classify_window(
            cron_managed=cron_managed,
            screen=screen,
            previous=prev,
        )

        log_path = None
        if agent_dir is not None:
            # Prefer run/*.log if present
            run_logs = sorted((agent_dir / "broca" / "run").glob("*.log")) if (agent_dir / "broca" / "run").is_dir() else []
            if run_logs:
                log_path = str(run_logs[0])
        cron_log = None
        if logs_root is not None:
            cand = logs_root / f"{name}-broca-cron.log"
            if cand.is_file():
                cron_log = str(cand)

        windows = [
            Window(
                id=win_id,
                label=f"broca-{name}",
                screen_name=screen_name,
                state=state,
                last_seen_pid=screen.pid if screen else None,
            )
        ]
        # Separate run-log pane when broca/run/*.log exists (PRD §3).
        if log_path:
            windows.append(
                Window(
                    id=f"{name}/run-log",
                    label="run log",
                    log_path=log_path,
                    state=WindowState.RUNNING if state in (
                        WindowState.RUNNING,
                        WindowState.RETURNED,
                    ) else state,
                )
            )
        if cron_log:
            windows.append(
                Window(
                    id=f"{name}/cron",
                    label="cron log",
                    log_path=cron_log,
                    state=WindowState.RUNNING if cron_managed else WindowState.UNMANAGED,
                )
            )

        agents.append(
            Agent(
                name=name,
                cron_managed=cron_managed,
                start_script=start_path,
                windows=windows,
            )
        )

    # System pseudo-agent: letta + smcp (and any other non-broca screens of interest)
    sys_windows: list[Window] = []
    for sys_name, label in (("letta", "letta"), ("smcp", "smcp")):
        screen = by_screen.get(sys_name)
        win_id = f"system/{sys_name}"
        cron_managed = f"__{sys_name}__" in managed or screen is not None
        # letta is always treated as present-when-running; still excluded from Active Now
        prev = previous.get(win_id)
        if screen is None and f"__{sys_name}__" not in managed:
            continue
        state = classify_window(
            cron_managed=True,
            screen=screen,
            previous=prev,
        )
        sys_windows.append(
            Window(
                id=win_id,
                label=label,
                screen_name=sys_name,
                state=state,
                last_seen_pid=screen.pid if screen else None,
            )
        )
    if sys_windows:
        agents.append(
            Agent(
                name="System",
                cron_managed=True,
                windows=sys_windows,
            )
        )

    # Drop any accidental inclusion of excluded-as-agent names (shouldn't happen).
    return [a for a in agents if a.name not in EXCLUDED_SCREEN_NAMES]
