"""Host backends: live (moya) and fixture (dev/tests)."""

from __future__ import annotations

import os
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path

from .activity import pick_active_now, scrollback_delta_is_noise_only
from .discovery import build_agents
from .metrics import METRICS, calling_from_ui_thread
from .models import (
    EXCLUDED_SCREEN_NAMES,
    NEVER_HARDCOPY,
    CrashEvent,
    HostSnapshot,
    WindowState,
)
from .parsers import parse_screen_list
from .state import is_failure

# Keep memory bounded — full Letta/Broca hardcopies can be 100KB–MB each.
SCROLLBACK_MAX_BYTES = 32_768
SCROLLBACK_MAX_LINES = 120
HARDCOPY_TIMEOUT_S = 1.5


def _tail_text(text: str, *, max_bytes: int = SCROLLBACK_MAX_BYTES, max_lines: int = SCROLLBACK_MAX_LINES) -> str:
    if not text:
        return ""
    if len(text) > max_bytes:
        text = text[-max_bytes:]
        # avoid starting mid-line
        nl = text.find("\n")
        if nl != -1 and nl < len(text) - 1:
            text = text[nl + 1 :]
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines)


def _tail_file(
    path: Path,
    *,
    max_bytes: int = SCROLLBACK_MAX_BYTES,
    max_lines: int = SCROLLBACK_MAX_LINES,
) -> str:
    """Read the tail of a log file without loading the whole thing."""
    try:
        if not path.is_file():
            return ""
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
            raw = f.read()
        return _tail_text(
            raw.decode("utf-8", errors="replace"),
            max_bytes=max_bytes,
            max_lines=max_lines,
        )
    except OSError:
        return ""


class HostBackend(ABC):
    @abstractmethod
    def snapshot(self) -> HostSnapshot:
        ...

    @abstractmethod
    def read_scrollback(self, screen_name: str) -> str:
        ...


class FixtureHost(HostBackend):
    """Fake host rooted at a fixture directory."""

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
        for agent in agents:
            for w in agent.windows:
                if w.screen_name:
                    sb = self.root / "scrollback" / f"{w.screen_name}.txt"
                    if sb.is_file():
                        w.last_scrollback = _tail_text(sb.read_text())
                        w.last_activity_epoch = sb.stat().st_mtime
                elif w.log_path:
                    p = Path(w.log_path)
                    if p.is_file():
                        text = _tail_file(p)
                        if text.strip():
                            if not scrollback_delta_is_noise_only(
                                w.last_scrollback, text
                            ):
                                w.last_activity_epoch = p.stat().st_mtime
                            w.last_scrollback = text
                        else:
                            w.last_scrollback = text
                # otto_bridge counts (fixture + live share this shape)
                if agent.name != "System" and w.screen_name and w.screen_name.startswith("broca-"):
                    bridge = self.root / "agents" / agent.name / "broca" / "run" / "otto_bridge"
                    if bridge.is_dir():
                        try:
                            inbox = bridge / "inbox"
                            outbox = bridge / "outbox"
                            w.bridge_inbox_count = (
                                sum(1 for f in inbox.iterdir() if f.is_file()) if inbox.is_dir() else 0
                            )
                            w.bridge_outbox_count = (
                                sum(1 for f in outbox.iterdir() if f.is_file()) if outbox.is_dir() else 0
                            )
                            # counts only — bridge mtime must not drive Active Now
                        except OSError:
                            pass
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

        return HostSnapshot(
            agents=agents,
            screens=screens,
            events=list(self._events[-50:]),
            cpu_percent=12.5,
            load_avg=(0.1, 0.1, 0.1),
            mem_used_gib=1.7,
            mem_total_gib=3.6,
            mem_percent=47.0,
            net_up_bps=1200.0,
            net_down_bps=4800.0,
        )

    def read_scrollback(self, screen_name: str) -> str:
        p = self.root / "scrollback" / f"{screen_name}.txt"
        if p.is_file():
            return _tail_text(p.read_text())
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
        self._scroll_cache: dict[str, tuple[float, str]] = {}
        self._cpu_primed = False
        # Hardcopy is expensive — throttle and skip excluded screens.
        self.hardcopy_interval_s = 2.0
        self._last_hardcopy_epoch = 0.0
        self._focus_screens: set[str] = set()
        self._last_alive_epoch: dict[str, float] = {}
        self._returned_at: dict[str, float] = {}
        # (epoch, bytes_sent, bytes_recv) for net rate
        self._prev_net: tuple[float, int, int] | None = None
        # Textual main-thread id when known — snapshot must not run there (#4061).
        self.ui_thread_ident: int | None = None

    def set_focus_screens(self, names: set[str]) -> None:
        """Prefer hardcopying these screens (selected agent + Active Now + letta)."""
        self._focus_screens = {n for n in names if n and n not in NEVER_HARDCOPY}
        # Dedicated bottom-right pane always watches Letta when it exists.
        self._focus_screens.add("letta")

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
            return r.stdout or r.stderr or ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def snapshot(self) -> HostSnapshot:
        import psutil

        t0 = time.perf_counter()
        if calling_from_ui_thread(self.ui_thread_ident):
            METRICS.note_host_io_on_ui_thread()

        now = time.time()
        do_hardcopy = (now - self._last_hardcopy_epoch) >= self.hardcopy_interval_s
        if do_hardcopy:
            self._last_hardcopy_epoch = now

        crontab = self._read_crontab()
        screens = parse_screen_list(self._read_screen_list())
        agents = build_agents(
            agents_root=self.agents_root,
            crontab_text=crontab,
            screens=screens,
            previous=self._prev_states,
            logs_root=self.logs_root,
            now_epoch=now,
        )

        # Decide which screens to hardcopy this tick.
        want: set[str] = set(self._focus_screens)
        for agent in agents:
            for w in agent.windows:
                if not w.screen_name:
                    continue
                if w.screen_name in NEVER_HARDCOPY:
                    continue
                # Letta is only hardcopied when explicitly focused (always is).
                if w.screen_name in EXCLUDED_SCREEN_NAMES and w.screen_name not in want:
                    continue
                if w.state in (WindowState.MISSING, WindowState.UNMANAGED, WindowState.DEAD):
                    continue
                # Always include broca-* when focused empty (first paint): sample a few.
                if not want and w.screen_name.startswith("broca-"):
                    want.add(w.screen_name)
                elif w.screen_name in want:
                    pass
        # Cap hardcopies per tick hard — moya is a 2-core box.
        # Prefer letta + focused screens when over cap.
        if len(want) > 4:
            preferred = [n for n in want if n == "letta" or n in self._focus_screens]
            rest = [n for n in want if n not in preferred]
            want = set((preferred + rest)[:4])

        for agent in agents:
            for w in agent.windows:
                if w.screen_name and w.screen_name in NEVER_HARDCOPY:
                    continue
                if (
                    do_hardcopy
                    and w.screen_name
                    and w.screen_name in want
                    and w.state not in (WindowState.MISSING, WindowState.UNMANAGED)
                ):
                    text = self.read_scrollback(w.screen_name)
                    stripped = text.strip()
                    if stripped:
                        prev_cached = self._scroll_cache.get(w.screen_name)
                        prev_text = (prev_cached[1] if prev_cached else "").strip()
                        if stripped != prev_text:
                            # Bridge/httpx noise must not bump activity epoch —
                            # that was flipping Active Now between Brocas.
                            # Letta pane is display-only — never drives Active Now.
                            if w.screen_name != "letta" and not scrollback_delta_is_noise_only(
                                prev_text, stripped
                            ):
                                w.last_activity_epoch = now
                        w.last_scrollback = text
                        self._scroll_cache[w.screen_name] = (now, text)
                elif w.screen_name and w.screen_name in self._scroll_cache:
                    # Reuse last good capture between hardcopy ticks.
                    _, cached = self._scroll_cache[w.screen_name]
                    w.last_scrollback = cached
                elif w.log_path:
                    p = Path(w.log_path)
                    if p.is_file():
                        # Always show a fresh log tail for cron/run panes.
                        text = _tail_file(p)
                        if text.strip():
                            # Poll noise (webchat "Retrieved 0 messages", etc.) must
                            # not bump last_activity — that made longfellow's age
                            # reset every ~3s while the log grew.
                            if not scrollback_delta_is_noise_only(
                                w.last_scrollback, text
                            ):
                                try:
                                    mtime = p.stat().st_mtime
                                    w.last_activity_epoch = max(
                                        w.last_activity_epoch, mtime
                                    )
                                except OSError:
                                    pass
                            w.last_scrollback = text

                # otto_bridge counts + mtime (file counts only — no DB)
                if agent.name != "System" and w.screen_name and w.screen_name.startswith("broca-"):
                    bridge = self.agents_root / agent.name / "broca" / "run" / "otto_bridge"
                    if bridge.is_dir():
                        latest = 0.0
                        inbox_n = outbox_n = 0
                        try:
                            for sub, counter in (("inbox", "in"), ("outbox", "out")):
                                d = bridge / sub
                                if d.is_dir():
                                    n = 0
                                    for f in d.iterdir():
                                        if f.is_file():
                                            n += 1
                                    if counter == "in":
                                        inbox_n = n
                                    else:
                                        outbox_n = n
                        except OSError:
                            pass
                        w.bridge_inbox_count = inbox_n
                        w.bridge_outbox_count = outbox_n
                        # Do NOT bump last_activity_epoch from bridge file mtimes —
                        # outbox churn flipped Active Now between Brocas constantly.

                # seconds missing since last alive
                if w.state in (WindowState.RUNNING, WindowState.RETURNED):
                    self._last_alive_epoch[w.id] = now
                    w.seconds_missing = 0.0
                elif is_failure(w.state):
                    last = self._last_alive_epoch.get(w.id)
                    w.seconds_missing = (now - last) if last else 0.0

                prev = self._prev_states.get(w.id)
                if prev is not None and not is_failure(prev) and is_failure(w.state):
                    self._events.append(CrashEvent(now, f"{w.label} died"))
                if prev is not None and is_failure(prev) and w.state == WindowState.RETURNED:
                    self._events.append(CrashEvent(now, f"{w.label} back"))
                    self._returned_at[w.id] = now
                if w.state == WindowState.RETURNED:
                    w.returned_at_epoch = self._returned_at.get(w.id, now)
                    # After 3s flash, treat as running for subsequent ticks.
                    if now - w.returned_at_epoch >= 3.0:
                        w.state = WindowState.RUNNING
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
        sent = int(net.bytes_sent) if net else 0
        recv = int(net.bytes_recv) if net else 0
        up_bps = down_bps = 0.0
        if self._prev_net is not None:
            prev_t, prev_s, prev_r = self._prev_net
            dt = max(0.001, now - prev_t)
            up_bps = max(0.0, (sent - prev_s) / dt)
            down_bps = max(0.0, (recv - prev_r) / dt)
        self._prev_net = (now, sent, recv)
        if not self._cpu_primed:
            psutil.cpu_percent(interval=None)
            self._cpu_primed = True
        # Never block the UI thread with interval>0.
        cpu = psutil.cpu_percent(interval=None)
        load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
        snap = HostSnapshot(
            agents=agents,
            screens=screens,
            events=list(self._events[-50:]),
            cpu_percent=cpu,
            load_avg=load,
            mem_used_gib=(vm.total - vm.available) / (1024**3),
            mem_total_gib=vm.total / (1024**3),
            mem_percent=float(vm.percent),
            net_bytes_sent=sent,
            net_bytes_recv=recv,
            net_up_bps=up_bps,
            net_down_bps=down_bps,
        )
        METRICS.record_snapshot(time.perf_counter() - t0)
        return snap

    def read_scrollback(self, screen_name: str) -> str:
        """Hardcopy into our tmp dir only — never attach, never quit.

        screen truncates the output file at the start of hardcopy, then writes.
        Reading the shared path mid-write yields an empty/partial dump, which
        made the TUI clear+rewrite the log every few ticks (false scrolling).
        We hardcopy to a unique path, wait for a non-empty stable size, and
        fall back to the last good cache on failure.
        """
        if screen_name in NEVER_HARDCOPY:
            return ""
        cached = self._scroll_cache.get(screen_name)
        cached_text = cached[1] if cached else ""
        out = self.tmp / f"{screen_name}.{time.time_ns()}.hc"
        t0 = time.perf_counter()
        try:
            subprocess.run(
                ["screen", "-S", screen_name, "-X", "hardcopy", "-h", str(out)],
                capture_output=True,
                text=True,
                timeout=HARDCOPY_TIMEOUT_S,
                check=False,
            )
            prev_size = -1
            size = 0
            for _ in range(25):  # ~250ms max
                try:
                    if out.is_file():
                        size = out.stat().st_size
                        if size > 0 and size == prev_size:
                            break
                        prev_size = size
                except OSError:
                    pass
                time.sleep(0.01)
            if size <= 0:
                return cached_text
            with out.open("rb") as f:
                if size > SCROLLBACK_MAX_BYTES:
                    f.seek(-SCROLLBACK_MAX_BYTES, os.SEEK_END)
                raw = f.read()
            text = _tail_text(raw.decode("utf-8", errors="replace"))
            # Reject obviously truncated tails when we already have a good buffer.
            if cached_text and text:
                cached_n = cached_text.count("\n")
                new_n = text.count("\n")
                if cached_n >= 40 and new_n < max(10, cached_n // 3):
                    return cached_text
            return text if text.strip() else cached_text
        except (OSError, subprocess.TimeoutExpired):
            return cached_text
        finally:
            METRICS.record_hardcopy(screen_name, time.perf_counter() - t0)
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass
