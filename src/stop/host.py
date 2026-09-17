"""Host backends: live (moya) and fixture (dev/tests)."""

from __future__ import annotations

import os
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path

from .discovery import build_agents
from .models import CrashEvent, HostSnapshot, WindowState
from .parsers import parse_screen_list
from .state import is_failure


class HostBackend(ABC):
    @abstractmethod
    def snapshot(self) -> HostSnapshot:
        ...

    @abstractmethod
    def read_scrollback(self, screen_name: str) -> str:
        ...


class FixtureHost(HostBackend):
    """Fake host rooted at a fixture directory.

    Layout:
      crontab.txt
      screen-list.txt          (or screen-list.N.txt for tick N)
      agents/<name>/...
      logs/<name>-broca-cron.log
      scrollback/<screen>.txt
      tick                     (optional int file; advanced by bump_tick)
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self._prev_states: dict[str, WindowState] = {}
        self._events: list[CrashEvent] = []
        self._tick = 0
        tick_file = self.root / "tick"
        if tick_file.is_file():
            try:
                self._tick = int(tick_file.read_text().strip() or "0")
            except ValueError:
                self._tick = 0

    def bump_tick(self) -> int:
        self._tick += 1
        (self.root / "tick").write_text(str(self._tick))
        return self._tick

    def _screen_text(self) -> str:
        numbered = self.root / f"screen-list.{self._tick}.txt"
        if numbered.is_file():
            return numbered.read_text()
        return (self.root / "screen-list.txt").read_text()

    def snapshot(self) -> HostSnapshot:
        crontab = (self.root / "crontab.txt").read_text()
        screens = parse_screen_list(self._screen_text())
        agents = build_agents(
            agents_root=self.root / "agents",
            crontab_text=crontab,
            screens=screens,
            previous=self._prev_states,
            logs_root=self.root / "logs",
            now_epoch=time.time(),
        )
        # Activity from scrollback mtimes / content length
        for agent in agents:
            for w in agent.windows:
                if w.screen_name:
                    sb = self.root / "scrollback" / f"{w.screen_name}.txt"
                    if sb.is_file():
                        w.last_scrollback = sb.read_text()
                        w.last_activity_epoch = sb.stat().st_mtime
                elif w.log_path:
                    p = Path(w.log_path)
                    if p.is_file():
                        w.last_activity_epoch = p.stat().st_mtime
                # Crash events
                prev = self._prev_states.get(w.id)
                if prev is not None and not is_failure(prev) and is_failure(w.state):
                    self._events.append(
                        CrashEvent(time.time(), f"{w.label} died")
                    )
                if prev is not None and is_failure(prev) and w.state == WindowState.RETURNED:
                    self._events.append(
                        CrashEvent(time.time(), f"{w.label} back")
                    )
                self._prev_states[w.id] = (
                    WindowState.RUNNING
                    if w.state == WindowState.RETURNED
                    else w.state
                )
                agent.last_activity_epoch = max(
                    agent.last_activity_epoch, w.last_activity_epoch
                )

        return HostSnapshot(
            agents=agents,
            screens=screens,
            events=list(self._events[-50:]),
            cpu_percent=12.5,
            load_avg=(0.1, 0.1, 0.1),
            mem_used_gib=1.7,
            mem_total_gib=3.6,
        )

    def read_scrollback(self, screen_name: str) -> str:
        p = self.root / "scrollback" / f"{screen_name}.txt"
        if p.is_file():
            return p.read_text()
        return ""


class LiveHost(HostBackend):
    """Real moya host. Read-only except hardcopy into /tmp/stop-<uid>/."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        agents_root: Path | None = None,
        logs_root: Path | None = None,
    ):
        self.home = Path(home or os.path.expanduser("~"))
        self.agents_root = agents_root or (self.home / "sanctum" / "agents")
        self.logs_root = logs_root or (self.home / "logs")
        self.tmp = Path(f"/tmp/stop-{os.getuid()}")
        self.tmp.mkdir(mode=0o700, exist_ok=True)
        self._prev_states: dict[str, WindowState] = {}
        self._events: list[CrashEvent] = []

    def _read_crontab(self) -> str:
        try:
            r = subprocess.run(
                ["crontab", "-l"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return r.stdout if r.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def _read_screen_list(self) -> str:
        try:
            r = subprocess.run(
                ["screen", "-ls"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            # screen -ls returns 1 when sessions exist on some versions
            return r.stdout or r.stderr or ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def snapshot(self) -> HostSnapshot:
        import psutil

        crontab = self._read_crontab()
        screens = parse_screen_list(self._read_screen_list())
        agents = build_agents(
            agents_root=self.agents_root,
            crontab_text=crontab,
            screens=screens,
            previous=self._prev_states,
            logs_root=self.logs_root,
            now_epoch=time.time(),
        )
        for agent in agents:
            for w in agent.windows:
                if w.screen_name and w.state not in (
                    WindowState.MISSING,
                    WindowState.UNMANAGED,
                ):
                    text = self.read_scrollback(w.screen_name)
                    stripped = text.strip()
                    if stripped:
                        if stripped != (w.last_scrollback or "").strip():
                            w.last_activity_epoch = time.time()
                        w.last_scrollback = text
                elif w.log_path:
                    p = Path(w.log_path)
                    if p.is_file():
                        w.last_activity_epoch = p.stat().st_mtime
                prev = self._prev_states.get(w.id)
                if prev is not None and not is_failure(prev) and is_failure(w.state):
                    self._events.append(CrashEvent(time.time(), f"{w.label} died"))
                if prev is not None and is_failure(prev) and w.state == WindowState.RETURNED:
                    self._events.append(CrashEvent(time.time(), f"{w.label} back"))
                self._prev_states[w.id] = (
                    WindowState.RUNNING
                    if w.state == WindowState.RETURNED
                    else w.state
                )
                agent.last_activity_epoch = max(
                    agent.last_activity_epoch, w.last_activity_epoch
                )

        vm = psutil.virtual_memory()
        net = psutil.net_io_counters()
        # Prime CPU counter so the first reading isn't a useless spike/zero.
        psutil.cpu_percent(interval=None)
        cpu = psutil.cpu_percent(interval=0.05)
        load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
        return HostSnapshot(
            agents=agents,
            screens=screens,
            events=list(self._events[-50:]),
            cpu_percent=cpu,
            load_avg=load,
            mem_used_gib=(vm.total - vm.available) / (1024**3),
            mem_total_gib=vm.total / (1024**3),
            net_bytes_sent=net.bytes_sent if net else 0,
            net_bytes_recv=net.bytes_recv if net else 0,
        )

    def read_scrollback(self, screen_name: str) -> str:
        """Hardcopy into our tmp dir only — never attach, never quit."""
        if screen_name in ("stop",):
            return ""
        out = self.tmp / f"{screen_name}.txt"
        try:
            subprocess.run(
                ["screen", "-S", screen_name, "-X", "hardcopy", "-h", str(out)],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if out.is_file():
                return out.read_text(errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            pass
        return ""
