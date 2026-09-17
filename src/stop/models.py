"""Domain models for stop."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class WindowState(str, Enum):
    """Lifecycle of a watched Sanctum window (screen session / surface)."""

    RUNNING = "running"
    DEAD = "dead"
    MISSING = "missing"
    UNMANAGED = "unmanaged"
    RETURNED = "returned"


# Sessions that must never appear as agent lanes / Active Now targets.
EXCLUDED_SCREEN_NAMES = frozenset({"stop", "letta"})


@dataclass(frozen=True)
class ScreenSession:
    """One line from `screen -list`."""

    name: str
    pid: int
    status: str  # Detached | Attached | Dead | ...


@dataclass
class Window:
    """A surface belonging to an agent (usually a screen session)."""

    id: str
    label: str
    screen_name: Optional[str] = None
    log_path: Optional[str] = None
    state: WindowState = WindowState.MISSING
    last_seen_pid: Optional[int] = None
    last_activity_epoch: float = 0.0
    seconds_missing: float = 0.0
    last_scrollback: str = ""
    bridge_inbox_count: int | None = None
    bridge_outbox_count: int | None = None
    returned_at_epoch: float = 0.0


@dataclass
class Agent:
    """One Sanctum agent (or the System pseudo-agent)."""

    name: str
    cron_managed: bool = False
    start_script: Optional[str] = None
    windows: list[Window] = field(default_factory=list)
    last_activity_epoch: float = 0.0

    @property
    def state(self) -> WindowState:
        if not self.windows:
            return WindowState.UNMANAGED if not self.cron_managed else WindowState.MISSING
        # Worst non-returned state among windows, preferring failure.
        order = (
            WindowState.DEAD,
            WindowState.MISSING,
            WindowState.RETURNED,
            WindowState.RUNNING,
            WindowState.UNMANAGED,
        )
        states = {w.state for w in self.windows}
        for s in order:
            if s in states:
                return s
        return WindowState.UNMANAGED


@dataclass
class CrashEvent:
    """In-memory event strip entry."""

    epoch: float
    message: str


@dataclass
class HostSnapshot:
    """One discovery tick."""

    agents: list[Agent]
    screens: list[ScreenSession]
    events: list[CrashEvent] = field(default_factory=list)
    cpu_percent: float = 0.0
    load_avg: tuple[float, float, float] = (0.0, 0.0, 0.0)
    mem_used_gib: float = 0.0
    mem_total_gib: float = 0.0
    mem_percent: float = 0.0
    net_bytes_sent: int = 0
    net_bytes_recv: int = 0
    # Instantaneous rates (bytes/sec), btop-style — not lifetime counters.
    net_up_bps: float = 0.0
    net_down_bps: float = 0.0
