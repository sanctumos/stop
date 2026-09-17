"""Textual TUI for stop."""

from __future__ import annotations

import os
import time
import traceback
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Static

from .activity import pick_active_now
from .config import StopConfig, apply_agent_order, load_config
from .host import FixtureHost, HostBackend, LiveHost
from .models import Agent, HostSnapshot, Window, WindowState
from .state import badge_label, is_failure, short_badge


def _state_style(state: WindowState) -> str:
    if is_failure(state):
        return "bold red"
    if state == WindowState.RETURNED:
        return "bold green"
    if state == WindowState.UNMANAGED:
        return "dim"
    return "green"


def _age(epoch: float, now: float | None = None) -> str:
    """Compact duration since epoch (activity age or uptime)."""
    if not epoch:
        return "-"
    now = now or time.time()
    sec = max(0, int(now - epoch))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    days = sec // 86400
    hours = (sec % 86400) // 3600
    if hours:
        return f"{days}d{hours}h"
    return f"{days}d"


def _age_stable(epoch: float, now: float | None = None) -> str:
    """Like _age, but sub-minute stays 'now' so the UI does not tick every second."""
    if not epoch:
        return "-"
    now = now or time.time()
    sec = max(0, int(now - epoch))
    if sec < 60:
        return "now"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    days = sec // 86400
    hours = (sec % 86400) // 3600
    if hours:
        return f"{days}d{hours}h"
    return f"{days}d"


def _paint(widget: Static, body: str, *, title: str | None = None) -> None:
    """Update a Static only when the body actually changed.

    Idle screens produce identical hardcopies; re-calling Static.update() every
    host tick still repaints and looks like the logs are scrolling. Bridge
    count twitches must not force a log redraw either — put those in `title`.
    """
    if title is not None and getattr(widget, "_paint_title", None) != title:
        widget.border_title = title
        widget._paint_title = title
    if getattr(widget, "_paint_body", None) == body:
        return
    widget._paint_body = body
    widget.update(body)


def _plain(text: str) -> str:
    """Make arbitrary log/UI text safe for Rich markup (escape ALL brackets)."""
    # rich.markup.escape only escapes tag-shaped [...] ; Broca logs also contain
    # stray closers like [/path] which raise MarkupError and kill the app.
    return text.replace("\\", "\\\\").replace("[", "\\[")


def _safe_lines(text: str, *, limit: int = 40, width: int = 120) -> list[str]:
    """Plain log lines safe for Rich markup rendering."""
    out = []
    for pl in (text or "").strip().splitlines()[-limit:]:
        cleaned = "".join(ch if ch >= " " or ch in "\t" else "?" for ch in pl)
        out.append(_plain(cleaned[:width]))
    return out


class HelpScreen(ModalScreen[None]):
    """Full help overlay (PRD `?`)."""

    BINDINGS = [Binding("escape", "dismiss_help", "close", show=False), Binding("q", "dismiss_help", "close", show=False), Binding("question_mark", "dismiss_help", "close", show=False)]

    def compose(self) -> ComposeResult:
        yield Static(
            "[b]stop — SanctumOS-top[/b]\n\n"
            "↑↓ / j k     select agent\n"
            "Enter        expand window full-height\n"
            "Esc          collapse / close help / cancel filter\n"
            "Tab          cycle panes (narrow: page agents→windows→Active Now)\n"
            "a            jump to Active Now\n"
            "f            follow-lock / release Active Now\n"
            "/            filter agents\n"
            "?            this help\n"
            "q            quit\n\n"
            "No restart button — cron restarts agents.\n"
            "Active Now follows human/agent dialogue — not otto_bridge chatter.\n"
            "Letta + stop screens excluded from Active Now.\n"
            "Log lines with [brackets] are escaped (cannot crash UI).\n\n"
            "[dim]Esc / q / ? to close[/dim]",
            id="help-body",
        )

    def action_dismiss_help(self) -> None:
        self.dismiss()

    def on_key(self, event) -> None:  # noqa: ANN001
        # Any key closes so Mark isn't trapped.
        if event.key not in ("up", "down", "left", "right"):
            self.dismiss()


_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def _spark(history: list[float], *, width: int = 24, vmax: float | None = None) -> str:
    """Render a sparkline from history samples (newest last)."""
    if not history:
        return "▁" * width
    samples = history[-width:]
    pad = width - len(samples)
    top = vmax if vmax and vmax > 0 else max(max(samples), 1e-9)
    out = []
    for v in samples:
        idx = int((max(0.0, min(v, top)) / top) * (len(_SPARK_BLOCKS) - 1))
        out.append(_SPARK_BLOCKS[idx])
    return "▁" * pad + "".join(out)


class HostStrip(Static):
    """btop-ish host meters: bars + rolling sparklines for CPU and net."""

    HISTORY = 60

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._cpu_hist: list[float] = []
        self._up_hist: list[float] = []
        self._down_hist: list[float] = []

    @staticmethod
    def _bar(pct: float, width: int = 14) -> str:
        pct = max(0.0, min(100.0, pct))
        filled = int(round((pct / 100.0) * width))
        return "■" * filled + "·" * (width - filled)

    @staticmethod
    def _rate(bps: float) -> str:
        if bps < 1024:
            return f"{bps:6.0f} B/s  "
        if bps < 1024 * 1024:
            return f"{bps / 1024:6.1f} KiB/s"
        return f"{bps / (1024 * 1024):6.2f} MiB/s"

    @staticmethod
    def _pressure_style(pct: float) -> str:
        return "green" if pct < 70 else ("yellow" if pct < 90 else "red")

    def show(self, snap: HostSnapshot) -> None:
        load = snap.load_avg
        mem_pct = snap.mem_percent
        if mem_pct <= 0 and snap.mem_total_gib > 0:
            mem_pct = 100.0 * snap.mem_used_gib / snap.mem_total_gib

        self._cpu_hist = (self._cpu_hist + [snap.cpu_percent])[-self.HISTORY:]
        self._up_hist = (self._up_hist + [snap.net_up_bps])[-self.HISTORY:]
        self._down_hist = (self._down_hist + [snap.net_down_bps])[-self.HISTORY:]

        cpu_style = self._pressure_style(snap.cpu_percent)
        ram_style = self._pressure_style(mem_pct)
        cpu_spark = _spark(self._cpu_hist, vmax=100.0)
        up_spark = _spark(self._up_hist)
        down_spark = _spark(self._down_hist)

        line1 = (
            f"[b]CPU[/b] [{cpu_style}]{self._bar(snap.cpu_percent)}[/{cpu_style}] "
            f"{snap.cpu_percent:5.1f}%  [{cpu_style}]{cpu_spark}[/{cpu_style}]  "
            f"load [b]{load[0]:.2f}[/b] {load[1]:.2f} {load[2]:.2f}"
        )
        line2 = (
            f"[b]RAM[/b] [{ram_style}]{self._bar(mem_pct)}[/{ram_style}] "
            f"{snap.mem_used_gib:4.1f}/{snap.mem_total_gib:.1f}G {mem_pct:3.0f}%  "
            f"[b]NET[/b] [cyan]↑{self._rate(snap.net_up_bps)}[/cyan] [cyan]{up_spark}[/cyan] "
            f"[magenta]↓{self._rate(snap.net_down_bps)}[/magenta] [magenta]{down_spark}[/magenta]"
        )
        self.update(line1 + "\n" + line2)


class AgentList(Static):
    can_focus = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.agents: list[Agent] = []
        self.index = 0
        self.filter = ""

    def set_agents(self, agents: list[Agent]) -> None:
        self.agents = agents
        if self.index >= len(self.visible()):
            self.index = max(0, len(self.visible()) - 1)
        self.refresh_view()

    def visible(self) -> list[Agent]:
        if not self.filter:
            return self.agents
        q = self.filter.lower()
        return [a for a in self.agents if q in a.name.lower()]

    def selected(self) -> Agent | None:
        vis = self.visible()
        if not vis:
            return None
        return vis[self.index]

    def move(self, delta: int) -> None:
        vis = self.visible()
        if not vis:
            return
        self.index = (self.index + delta) % len(vis)
        self.refresh_view()

    def refresh_view(self) -> None:
        now = time.time()
        lines = []
        for i, a in enumerate(self.visible()):
            mark = ">" if i == self.index else " "
            style = _state_style(a.state)
            badge = short_badge(a.state)
            # Agent list shows real screen uptime — not last_activity (that was
            # resetting every few seconds when a chatty run log kept writing).
            uptime = _age(a.started_at_epoch, now)
            if a.state == WindowState.UNMANAGED and not a.started_at_epoch:
                uptime = "-"
            # Compact one-liner for ~28 usable cols.
            lines.append(f"{mark} [{style}]{a.name}[/{style}] {badge} {uptime}")
        title = "agents"
        if self.filter:
            title += f" /{self.filter}"
        _paint(self, "\n".join(lines) if lines else "(none)", title=title)


class WindowPane(Static):
    can_focus = True
    expanded = False

    @staticmethod
    def _bridge_title_bit(w: Window) -> str:
        if w.bridge_inbox_count is None:
            return ""
        return f" · bridge in={w.bridge_inbox_count} out={w.bridge_outbox_count or 0}"

    def show_agent(self, agent: Agent | None, win_index: int = 0) -> None:
        self._agent = agent
        self._win_index = win_index
        if agent is None:
            _paint(self, "(select an agent)", title="windows")
            return
        if self.expanded and agent.windows:
            w = agent.windows[win_index % len(agent.windows)]
            style = _state_style(w.state)
            title = (
                f"{agent.name} / {w.label}{self._bridge_title_bit(w)} — Esc to collapse"
            )
            lines = [f"[{style}]{badge_label(w.state)}[/{style}]"]
            if is_failure(w.state):
                miss = f" — missing {int(w.seconds_missing)}s" if w.seconds_missing else ""
                lines.append(f"[red]waiting for supervisor…{miss}[/red]")
            lines.extend(_safe_lines(w.last_scrollback, limit=60, width=160))
            _paint(
                self,
                "\n".join(lines) if lines else "(empty)",
                title=title,
            )
            return
        # Bridge counts live in the title so outbox twitches don't repaint logs.
        broca = next(
            (w for w in agent.windows if (w.screen_name or "").startswith("broca-")),
            None,
        )
        bit = self._bridge_title_bit(broca) if broca else ""
        title = f"{agent.name} windows{bit} — Enter to expand"
        lines: list[str] = []
        for i, w in enumerate(agent.windows):
            selected = i == win_index
            mark = ">" if selected else " "
            style = _state_style(w.state)
            badge = badge_label(w.state)
            pid = f" pid={w.last_seen_pid}" if w.last_seen_pid else ""
            miss = ""
            if is_failure(w.state) and w.seconds_missing:
                miss = f" {int(w.seconds_missing)}s"
            lines.append("")
            lines.append(
                f"{mark} [{style}]{_plain(w.label)}[/{style}] {badge}{miss}[dim]{pid}[/dim]"
            )
            if is_failure(w.state):
                lines.append("    [red]waiting for supervisor…[/red]")
            # Selected window gets a real log view; others a short teaser.
            depth = 14 if selected else 2
            for pl in _safe_lines(w.last_scrollback, limit=depth, width=150):
                lines.append(f"    {pl}")
        _paint(self, "\n".join(lines), title=title)


class ActiveNowPane(Static):
    can_focus = True

    def show(self, window: Window | None, locked: bool) -> None:
        from .activity import KIND_DIALOGUE, KIND_OTHER, classify_scrollback

        lock = " · LOCKED" if locked else ""
        if window is None:
            _paint(
                self,
                "[dim](idle — no agent console has produced output yet)[/dim]",
                title=f"Active Now{lock}",
            )
            return
        style = _state_style(window.state)
        age = _age_stable(window.last_activity_epoch)
        kind, hits, _ = classify_scrollback(window.last_scrollback)
        if kind == KIND_DIALOGUE:
            kind_bit = f"dialogue×{hits}"
        elif kind == KIND_OTHER:
            kind_bit = "signal"
        else:
            kind_bit = "quiet"
        # Volatile bits (age, kind) stay in the title so the log body only
        # repaints when scrollback text actually changes.
        title = f"Active Now — {window.label} · {kind_bit} · {age}{lock}"
        lines = [f"[{style}]{badge_label(window.state)}[/{style}]"]
        preview = _safe_lines(window.last_scrollback, limit=16, width=160)
        lines.extend(preview if preview else ["(no scrollback yet)"])
        _paint(self, "\n".join(lines), title=title)


class EventStrip(Static):
    def show(self, snap: HostSnapshot) -> None:
        if not snap.events:
            _paint(self, "events: (none)")
            return
        bits = [_plain(e.message) for e in snap.events[-5:]]
        _paint(self, "events: " + " · ".join(bits))


class StopApp(App[None]):
    TITLE = "stop"
    SUB_TITLE = "SanctumOS-top"
    CSS = """
    Screen { layout: vertical; }
    #host { height: 2; dock: top; background: $boost; padding: 0 1; }
    #events { height: 1; dock: bottom; color: $text-muted; padding: 0 1; }
    #body { height: 1fr; }
    #row { height: 2fr; }
    #agents {
        width: 30; height: 100%;
        border: round $accent; border-title-align: left;
        padding: 0 1;
    }
    #windows {
        width: 1fr; height: 100%;
        border: round $primary;
        padding: 0 1;
        overflow-y: hidden;
    }
    #active {
        height: 1fr; min-height: 12;
        border: round $warning;
        padding: 0 1;
        overflow-y: hidden;
    }
    #agents:focus, #windows:focus, #active:focus { border: round $success; }
    #filter { dock: bottom; display: none; height: 3; }
    #filter.visible { display: block; }

    HelpScreen { align: center middle; background: $background 80%; }
    #help-body {
        width: 64;
        height: auto;
        max-height: 90%;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
    }

    Screen.medium #agents { width: 26; }

    Screen.narrow #row { layout: vertical; }
    Screen.narrow #agents { width: 1fr; height: 1fr; }
    Screen.narrow #windows { width: 1fr; height: 1fr; display: none; }
    Screen.narrow #active { height: 1fr; display: none; }
    Screen.narrow.page-windows #agents { display: none; }
    Screen.narrow.page-windows #windows { display: block; }
    Screen.narrow.page-active #agents { display: none; }
    Screen.narrow.page-active #active { display: block; height: 1fr; }

    Screen.tiny #host { display: none; }
    """
    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("j,down", "down", "down", show=False),
        Binding("k,up", "up", "up", show=False),
        Binding("enter", "expand", "expand"),
        Binding("escape", "collapse", "collapse", show=False),
        Binding("tab", "cycle", "cycle", show=False),
        Binding("a", "focus_active", "active"),
        Binding("f", "toggle_follow", "follow"),
        Binding("question_mark", "help", "help"),
        Binding("slash", "filter", "filter"),
    ]

    def __init__(self, host: HostBackend, config: StopConfig | None = None):
        super().__init__()
        self.host = host
        self.config = config or StopConfig()
        self.follow_lock_id: str | None = None
        self._filter = ""
        self._snap: HostSnapshot | None = None
        self._win_index = 0
        self._narrow_page = "agents"
        self._errors = 0
        if isinstance(self.host, LiveHost):
            self.host.hardcopy_interval_s = float(self.config.refresh_hardcopy_s)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield HostStrip(id="host")
        with Vertical(id="body"):
            with Horizontal(id="row"):
                yield AgentList(id="agents")
                yield WindowPane(id="windows")
            yield ActiveNowPane(id="active")
        yield EventStrip(id="events")
        yield Input(placeholder="filter agents… (Enter apply, Esc cancel)", id="filter")
        yield Footer()

    def on_mount(self) -> None:
        interval = max(1.0, float(self.config.refresh_host_s))
        self.set_interval(interval, self.refresh_host)
        self.refresh_host()
        self.query_one(AgentList).focus()
        self._apply_breakpoint()

    def on_resize(self, event) -> None:  # noqa: ANN001
        self._apply_breakpoint()

    def _apply_breakpoint(self) -> None:
        size = self.size
        screen = self.screen
        screen.remove_class("medium", "narrow", "tiny", "page-windows", "page-active")
        if size.height < 24:
            screen.add_class("tiny")
        if size.width < 80:
            screen.add_class("narrow")
            if self._narrow_page == "windows":
                screen.add_class("page-windows")
            elif self._narrow_page == "active":
                screen.add_class("page-active")
        elif size.width < 120:
            screen.add_class("medium")

    def _update_focus_screens(self, selected: Agent | None, active: Window | None) -> None:
        if not isinstance(self.host, LiveHost):
            return
        names: set[str] = set()
        if selected:
            for w in selected.windows:
                if w.screen_name:
                    names.add(w.screen_name)
        if active and active.screen_name:
            names.add(active.screen_name)
        self.host.set_focus_screens(names)

    def refresh_host(self) -> None:
        try:
            agents_w = self.query_one(AgentList)
            selected = agents_w.selected() if self._snap else None
            active_guess = None
            if self._snap:
                active_guess = pick_active_now(
                    self._snap.agents,
                    follow_lock_id=self.follow_lock_id,
                    exclude_screens=frozenset(self.config.active_exclude),
                )
            self._update_focus_screens(selected, active_guess)

            snap = self.host.snapshot()
            snap.agents = apply_agent_order(snap.agents, self.config)
            self._snap = snap
            self.query_one(HostStrip).show(snap)
            agents_w.filter = self._filter
            agents_w.set_agents(snap.agents)
            selected = agents_w.selected()
            self.query_one(WindowPane).show_agent(selected, self._win_index)
            active = pick_active_now(
                snap.agents,
                follow_lock_id=self.follow_lock_id,
                exclude_screens=frozenset(self.config.active_exclude),
            )
            self._update_focus_screens(selected, active)
            self.query_one(ActiveNowPane).show(
                active, locked=self.follow_lock_id is not None
            )
            self.query_one(EventStrip).show(snap)
            self._errors = 0
        except Exception as exc:  # noqa: BLE001 — keep TUI alive
            self._errors += 1
            self.query_one(EventStrip).update(
                f"[red]refresh error ({self._errors}): {_plain(str(exc)[:80])}[/red]"
            )
            try:
                log = Path(f"/tmp/stop-{os.getuid()}") / "tui-errors.log"
                log.parent.mkdir(mode=0o700, exist_ok=True)
                with log.open("a", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + traceback.format_exc() + "\n")
            except OSError:
                pass

    def action_down(self) -> None:
        pane = self.query_one(WindowPane)
        if pane.expanded and self.query_one(AgentList).selected():
            agent = self.query_one(AgentList).selected()
            if agent and agent.windows:
                self._win_index = (self._win_index + 1) % len(agent.windows)
                pane.show_agent(agent, self._win_index)
                return
        self.query_one(AgentList).move(1)
        self._win_index = 0
        self._sync_windows()
        self.refresh_host()

    def action_up(self) -> None:
        pane = self.query_one(WindowPane)
        if pane.expanded and self.query_one(AgentList).selected():
            agent = self.query_one(AgentList).selected()
            if agent and agent.windows:
                self._win_index = (self._win_index - 1) % len(agent.windows)
                pane.show_agent(agent, self._win_index)
                return
        self.query_one(AgentList).move(-1)
        self._win_index = 0
        self._sync_windows()
        self.refresh_host()

    def _sync_windows(self) -> None:
        if self._snap is None:
            return
        agents_w = self.query_one(AgentList)
        self.query_one(WindowPane).show_agent(agents_w.selected(), self._win_index)

    def action_expand(self) -> None:
        filt = self.query_one("#filter", Input)
        if "visible" in filt.classes:
            return
        pane = self.query_one(WindowPane)
        pane.expanded = True
        self._sync_windows()
        if "narrow" in self.screen.classes:
            self._narrow_page = "windows"
            self._apply_breakpoint()

    def action_collapse(self) -> None:
        filt = self.query_one("#filter", Input)
        if "visible" in filt.classes:
            filt.remove_class("visible")
            filt.value = ""
            self.query_one(AgentList).focus()
            return
        pane = self.query_one(WindowPane)
        pane.expanded = False
        self._sync_windows()

    def action_cycle(self) -> None:
        if "narrow" in self.screen.classes:
            order = ["agents", "windows", "active"]
            i = order.index(self._narrow_page)
            self._narrow_page = order[(i + 1) % len(order)]
            self._apply_breakpoint()
            return
        focused = self.focused
        sequence = [AgentList, WindowPane, ActiveNowPane]
        if focused is None:
            self.query_one(AgentList).focus()
            return
        for i, cls in enumerate(sequence):
            if isinstance(focused, cls):
                self.query_one(sequence[(i + 1) % len(sequence)]).focus()
                return
        self.query_one(AgentList).focus()

    def action_toggle_follow(self) -> None:
        if self._snap is None:
            return
        if self.follow_lock_id is not None:
            self.follow_lock_id = None
        else:
            active = pick_active_now(
                self._snap.agents,
                exclude_screens=frozenset(self.config.active_exclude),
            )
            self.follow_lock_id = active.id if active else None
        self.refresh_host()

    def action_focus_active(self) -> None:
        if "narrow" in self.screen.classes:
            self._narrow_page = "active"
            self._apply_breakpoint()
        self.query_one(ActiveNowPane).focus()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_filter(self) -> None:
        filt = self.query_one("#filter", Input)
        filt.add_class("visible")
        filt.value = self._filter
        filt.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "filter":
            return
        self._filter = event.value.strip()
        event.input.remove_class("visible")
        self.query_one(AgentList).focus()
        self.refresh_host()


def run_app(*, fixture: str | None = None, config_path: str | None = None) -> None:
    cfg = load_config(Path(config_path) if config_path else None)
    if fixture:
        host: HostBackend = FixtureHost(Path(fixture))
    else:
        host = LiveHost()
    StopApp(host, config=cfg).run()
