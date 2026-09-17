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
from .state import badge_label, is_failure


def _state_style(state: WindowState) -> str:
    if is_failure(state):
        return "bold red"
    if state == WindowState.RETURNED:
        return "bold green"
    if state == WindowState.UNMANAGED:
        return "dim"
    return "green"


def _age(epoch: float, now: float | None = None) -> str:
    if not epoch:
        return "-"
    now = now or time.time()
    sec = max(0, int(now - epoch))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h"


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


class HostStrip(Static):
    def show(self, snap: HostSnapshot) -> None:
        load = snap.load_avg
        self.update(
            f"CPU {snap.cpu_percent:5.1f}%  "
            f"load {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}  "
            f"RAM {snap.mem_used_gib:.1f}/{snap.mem_total_gib:.1f} GiB  "
            f"net ↑{snap.net_bytes_sent // 1024}k ↓{snap.net_bytes_recv // 1024}k"
        )


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
            badge = badge_label(a.state)
            age = _age(a.last_activity_epoch, now)
            # Compact one-liner: name · badge · age (must fit ~28 cols).
            lines.append(f"{mark} [{style}]{a.name}[/{style}] {badge} · {age}")
        title = "agents"
        if self.filter:
            title += f" /{_plain(self.filter)}"
        body = "\n".join(lines) if lines else "(none)"
        self.update(f"[b]{title}[/b]\n{body}")


class WindowPane(Static):
    can_focus = True
    expanded = False

    def show_agent(self, agent: Agent | None, win_index: int = 0) -> None:
        self._agent = agent
        self._win_index = win_index
        if agent is None:
            self.update("[b]windows[/b]\n(select an agent)")
            return
        if self.expanded and agent.windows:
            w = agent.windows[win_index % len(agent.windows)]
            style = _state_style(w.state)
            lines = [
                f"[b]{_plain(agent.name)} / {_plain(w.label)}[/b] "
                f"[{style}]{badge_label(w.state)}[/{style}]  (Esc to collapse)"
            ]
            if is_failure(w.state):
                miss = f" — missing {int(w.seconds_missing)}s" if w.seconds_missing else ""
                lines.append(f"[red]waiting for supervisor…{miss}[/red]")
            if w.bridge_inbox_count is not None:
                lines.append(
                    f"otto bridge  inbox={w.bridge_inbox_count}  "
                    f"outbox={w.bridge_outbox_count or 0}"
                )
            lines.extend(_safe_lines(w.last_scrollback, limit=40, width=140))
            self.update("\n".join(lines) if lines else "(empty)")
            return
        lines = [f"[b]{_plain(agent.name)} windows[/b]"]
        for i, w in enumerate(agent.windows):
            mark = ">" if i == win_index else " "
            style = _state_style(w.state)
            badge = badge_label(w.state)
            pid = f" pid={w.last_seen_pid}" if w.last_seen_pid else ""
            miss = ""
            if is_failure(w.state) and w.seconds_missing:
                miss = f" {int(w.seconds_missing)}s"
            lines.append(
                f"{mark} [{style}]{_plain(w.label)}[/{style}] {badge}{miss}{pid}"
            )
            if is_failure(w.state):
                lines.append("    [red]waiting for supervisor…[/red]")
            if w.bridge_inbox_count is not None:
                lines.append(
                    f"    otto bridge  inbox={w.bridge_inbox_count}  "
                    f"outbox={w.bridge_outbox_count or 0}"
                )
            for pl in _safe_lines(w.last_scrollback, limit=4, width=100):
                lines.append(f"    {pl}")
        self.update("\n".join(lines))


class ActiveNowPane(Static):
    can_focus = True

    def show(self, window: Window | None, locked: bool) -> None:
        lock = " [yellow]LOCKED[/yellow]" if locked else ""
        if window is None:
            self.update(f"[b]Active Now[/b]{lock}\n(idle)")
            return
        style = _state_style(window.state)
        age = _age(window.last_activity_epoch)
        lines = [
            f"[b]Active Now[/b]{lock} — [{style}]{_plain(window.label)}[/{style}] "
            f"{badge_label(window.state)}  age {age}"
        ]
        preview = _safe_lines(window.last_scrollback, limit=12, width=120)
        lines.extend(preview if preview else ["(no scrollback yet)"])
        self.update("\n".join(lines))


class EventStrip(Static):
    def show(self, snap: HostSnapshot) -> None:
        if not snap.events:
            self.update("events: (none)")
            return
        bits = [_plain(e.message) for e in snap.events[-5:]]
        self.update("events: " + " · ".join(bits))


class StopApp(App[None]):
    TITLE = "stop"
    SUB_TITLE = "SanctumOS-top"
    CSS = """
    Screen { layout: vertical; }
    #host { height: 1; dock: top; background: $boost; padding: 0 1; }
    #events { height: 1; dock: bottom; color: $text-muted; padding: 0 1; }
    #body { height: 1fr; }
    #row { height: 1fr; }
    #agents { width: 32; border: solid $accent; padding: 0 1; }
    #windows { width: 1fr; border: solid $primary; padding: 0 1; }
    #active { height: 14; border: solid $warning; padding: 0 1; }
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

    Screen.medium #agents { width: 28; }
    Screen.medium #active { height: 10; }

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
