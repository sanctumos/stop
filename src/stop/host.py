"""Host backends: live (moya) and fixture (dev/tests)."""

from __future__ import annotations

import os
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path

from .activity import (
    next_activity_epoch,
    pick_active_now,
    scrollback_delta_is_noise_only,
)
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
        self._activity_epoch: dict[str, float] = {}
        self._cpu_primed = False
        # Hardcopy is expensive — throttle and skip excluded screens.
        self.hardcopy_interval_s = 2.0
        self.hardcopy_batch_budget_s = 2.0
        self.hardcopy_max_screens = 6
        self.hardcopy_probe_slots = 3
        self._hardcopy_probe_cursor = 0
        self._last_hardcopy_epoch = 0.0
        # Ordered focus: selected Broca → Active Now → Letta → secondary (#4062).
        self._focus_order: list[str] = []
        self._last_alive_epoch: dict[str, float] = {}
        self._returned_at: dict[str, float] = {}
        # Bridge dir counts: cache so we do not iterdir every tick.
        self._bridge_count_cache: dict[str, tuple[float, int, int]] = {}
        self._bridge_count_interval_s = 4.0
        # (epoch, bytes_sent, bytes_recv) for net rate
        self._prev_net: tuple[float, int, int] | None = None
        # Textual main-thread id when known — snapshot must not run there (#4061).
        self.ui_thread_ident: int | None = None

    def set_focus_screens(self, names) -> None:
        """Set hardcopy priority order (deduped). Accepts list or set."""
        ordered: list[str] = []
        seen: set[str] = set()
        for n in names:
            if not n or n in NEVER_HARDCOPY or n in seen:
                continue
            ordered.append(n)
            seen.add(n)
        if "letta" not in seen:
            ordered.append("letta")
        self._focus_order = ordered

    @staticmethod
    def prioritize_hardcopy(
        focus_order: list[str],
        *,
        available: set[str],
        max_screens: int = 4,
    ) -> list[str]:
        """Deterministic hardcopy order: keep focus priority, drop missing, cap."""
        out: list[str] = []
        seen: set[str] = set()
        for n in focus_order:
            if n in NEVER_HARDCOPY or n in seen:
                continue
            if n not in available:
                continue
            out.append(n)
            seen.add(n)
            if len(out) >= max_screens:
                return out
        return out

    @staticmethod
    def plan_hardcopy_with_probes(
        focus_order: list[str],
        *,
        available: set[str],
        max_screens: int,
        probe_slots: int,
        probe_cursor: int,
    ) -> tuple[list[str], int]:
        """Keep focused panes hot while round-robin sampling other Brocas."""
        if max_screens <= 0:
            return [], probe_cursor
        reserved = min(max(0, probe_slots), max(0, max_screens - 1))
        primary_limit = max_screens - reserved
        out = LiveHost.prioritize_hardcopy(
            focus_order,
            available=available,
            max_screens=primary_limit,
        )
        seen = set(out)
        brocas = sorted(
            n
            for n in available
            if n.startswith("broca-") and n not in NEVER_HARDCOPY
        )
        cursor = probe_cursor
        probes_added = 0
        attempts = 0
        while brocas and attempts < len(brocas) and probes_added < reserved:
            name = brocas[cursor % len(brocas)]
            cursor += 1
            attempts += 1
            if name in seen:
                continue
            out.append(name)
            seen.add(name)
            probes_added += 1
        # If fewer probes exist than reserved slots, retain additional focused
        # secondary windows rather than wasting the hardcopy budget.
        for name in focus_order:
            if len(out) >= max_screens:
                break
            if name in available and name not in seen and name not in NEVER_HARDCOPY:
                out.append(name)
                seen.add(name)
        return out, cursor

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
        # Completion timestamp is set AFTER the batch — not before (#4062).

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

        available: set[str] = set()
        for agent in agents:
            for w in agent.windows:
                if not w.screen_name or w.screen_name in NEVER_HARDCOPY:
                    continue
                if w.state in (WindowState.MISSING, WindowState.UNMANAGED, WindowState.DEAD):
                    continue
                available.add(w.screen_name)

        focus = list(self._focus_order)
        if not any(n.startswith("broca-") for n in focus):
            # Cold start: sample a few brocas in stable name order.
            for n in sorted(s for s in available if s.startswith("broca-")):
                if n not in focus:
                    focus.append(n)
        want_list, next_probe_cursor = self.plan_hardcopy_with_probes(
            focus,
            available=available,
            max_screens=self.hardcopy_max_screens,
            probe_slots=self.hardcopy_probe_slots,
            probe_cursor=self._hardcopy_probe_cursor,
        )

        # Prune scroll + bridge caches for gone screens.
        alive_names = {s.name for s in screens}
        for stale in [k for k in self._scroll_cache if k not in alive_names]:
            self._scroll_cache.pop(stale, None)
            self._activity_epoch.pop(stale, None)
        for stale in [k for k in self._bridge_count_cache if k not in alive_names]:
            self._bridge_count_cache.pop(stale, None)

        batch_deadline = time.perf_counter() + self.hardcopy_batch_budget_s
        hardcopied: set[str] = set()
        if do_hardcopy:
            for screen_name in want_list:
                if time.perf_counter() >= batch_deadline:
                    break
                # Apply to every window sharing this screen name.
                for agent in agents:
                    for w in agent.windows:
                        if w.screen_name != screen_name:
                            continue
                        text = self.read_scrollback(w.screen_name)
                        stripped = text.strip()
                        if stripped:
                            prev_cached = self._scroll_cache.get(w.screen_name)
                            prev_text = (prev_cached[1] if prev_cached else "").strip()
                            if w.screen_name != "letta":
                                activity = next_activity_epoch(
                                    self._activity_epoch.get(w.screen_name, 0.0),
                                    prev_text,
                                    stripped,
                                    now=now,
                                )
                                self._activity_epoch[w.screen_name] = activity
                                w.last_activity_epoch = activity
                        w.last_scrollback = text
                        self._scroll_cache[w.screen_name] = (now, text)
                        hardcopied.add(screen_name)
            self._last_hardcopy_epoch = time.time()
            self._hardcopy_probe_cursor = next_probe_cursor

        for agent in agents:
            for w in agent.windows:
                if w.screen_name and w.screen_name in NEVER_HARDCOPY:
                    continue
                if w.screen_name and w.screen_name not in hardcopied:
                    if w.screen_name in self._scroll_cache:
                        _, cached = self._scroll_cache[w.screen_name]
                        w.last_scrollback = cached
                        w.last_activity_epoch = self._activity_epoch.get(
                            w.screen_name, 0.0
                        )
                    elif w.log_path:
                        p = Path(w.log_path)
                        if p.is_file():
                            text = _tail_file(p)
                            if text.strip():
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

                # otto_bridge counts — cached; not every tick.
                if agent.name != "System" and w.screen_name and w.screen_name.startswith("broca-"):
                    inbox_n, outbox_n = self._bridge_counts(agent.name, now)
                    w.bridge_inbox_count = inbox_n
                    w.bridge_outbox_count = outbox_n

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
                    self._events = self._events[-50:]
                if prev is not None and is_failure(prev) and w.state == WindowState.RETURNED:
                    self._events.append(CrashEvent(now, f"{w.label} back"))
                    self._events = self._events[-50:]
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

    def _bridge_counts(self, agent_name: str, now: float) -> tuple[int, int]:
        cached = self._bridge_count_cache.get(agent_name)
        if cached and (now - cached[0]) < self._bridge_count_interval_s:
            return cached[1], cached[2]
        bridge = self.agents_root / agent_name / "broca" / "run" / "otto_bridge"
        inbox_n = outbox_n = 0
        if bridge.is_dir():
            try:
                for sub, counter in (("inbox", "in"), ("outbox", "out")):
                    d = bridge / sub
                    if d.is_dir():
                        n = sum(1 for f in d.iterdir() if f.is_file())
                        if counter == "in":
                            inbox_n = n
                        else:
                            outbox_n = n
            except OSError:
                pass
        self._bridge_count_cache[agent_name] = (now, inbox_n, outbox_n)
        return inbox_n, outbox_n

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
            # A completed, nonzero hardcopy containing only whitespace is a
            # legitimate blank screen (commonly stdout redirected to a file).
            # It must clear this screen's old cache, not resurrect it.
            if not text.strip():
                return text
            # Reject obviously truncated tails when we already have a good buffer.
            if cached_text and text:
                cached_n = cached_text.count("\n")
                new_n = text.count("\n")
                if cached_n >= 40 and new_n < max(10, cached_n // 3):
                    return cached_text
            return text
        except (OSError, subprocess.TimeoutExpired):
            return cached_text
        finally:
            METRICS.record_hardcopy(screen_name, time.perf_counter() - t0)
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass
