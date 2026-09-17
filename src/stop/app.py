"""Textual TUI for stop."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Static

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
        lines = []
        for i, a in enumerate(self.visible()):
            mark = ">" if i == self.index else " "
            style = _state_style(a.state)
            badge = badge_label(a.state)
            lines.append(f"{mark} [{style}]{a.name:<12}[/{style}] {badge}")
        title = "agents"
        if self.filter:
            title += f" /{self.filter}"
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
                f"[b]{agent.name} / {w.label}[/b] [{style}]{badge_label(w.state)}[/{style}]  (Esc to collapse)"
            ]
            if is_failure(w.state):
                lines.append("[red]waiting for supervisor…[/red]")
            preview = (w.last_scrollback or "").strip().splitlines()
            lines.extend(pl[:140] for pl in preview[-40:])
            self.update("\n".join(lines) if lines else "(empty)")
            return
        lines = [f"[b]{agent.name} windows[/b]"]
        for i, w in enumerate(agent.windows):
            mark = ">" if i == win_index else " "
            style = _state_style(w.state)
            badge = badge_label(w.state)
            pid = f" pid={w.last_seen_pid}" if w.last_seen_pid else ""
            lines.append(f"{mark} [{style}]{w.label}[/{style}] {badge}{pid}")
            preview = (w.last_scrollback or "").strip().splitlines()
            if is_failure(w.state):
                lines.append("    [red]waiting for supervisor…[/red]")
            for pl in preview[-3:]:
                lines.append(f"    {pl[:100]}")
        self.update("\n".join(lines))


class ActiveNowPane(Static):
    can_focus = True

    def show(self, window: Window | None, locked: bool) -> None:
        lock = " [yellow]LOCKED[/yellow]" if locked else ""
        if window is None:
            self.update(f"[b]Active Now[/b]{lock}\n(idle)")
            return
        style = _state_style(window.state)
        lines = [
            f"[b]Active Now[/b]{lock} — [{style}]{window.label}[/{style}] {badge_label(window.state)}"
        ]
        preview = (window.last_scrollback or "").strip().splitlines()
        for pl in preview[-10:]:
            lines.append(pl[:120])
        if not preview:
            lines.append("(no scrollback yet)")
        self.update("\n".join(lines))


class EventStrip(Static):
    def show(self, snap: HostSnapshot) -> None:
        if not snap.events:
            self.update("events: (none)")
            return
        bits = [e.message for e in snap.events[-5:]]
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
    #agents { width: 28; border: solid $accent; padding: 0 1; }
    #windows { width: 1fr; border: solid $primary; padding: 0 1; }
    #active { height: 12; border: solid $warning; padding: 0 1; }

    /* medium: stack windows above active inside body already; shrink agents */
    Screen.medium #agents { width: 22; }
    Screen.medium #active { height: 10; }

    /* narrow / Termux: single column — hide non-focused panes via classes */
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
        self._narrow_page = "agents"  # agents | windows | active

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield HostStrip(id="host")
        with Vertical(id="body"):
            with Horizontal(id="row"):
                yield AgentList(id="agents")
                yield WindowPane(id="windows")
            yield ActiveNowPane(id="active")
        yield EventStrip(id="events")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(self.config.refresh_host_s, self.refresh_host)
        self.refresh_host()
        self.query_one(AgentList).focus()
        self._apply_breakpoint()

    def on_resize(self, event) -> None:  # noqa: ANN001
        self._apply_breakpoint()

    def _apply_breakpoint(self) -> None:
        size = self.size
        self.remove_class("medium", "narrow", "tiny", "page-windows", "page-active")
        if size.height < 24:
            self.add_class("tiny")
        if size.width < 80:
            self.add_class("narrow")
            if self._narrow_page == "windows":
                self.add_class("page-windows")
            elif self._narrow_page == "active":
                self.add_class("page-active")
        elif size.width < 120:
            self.add_class("medium")

    def refresh_host(self) -> None:
        snap = self.host.snapshot()
        snap.agents = apply_agent_order(snap.agents, self.config)
        self._snap = snap
        self.query_one(HostStrip).show(snap)
        agents_w = self.query_one(AgentList)
        agents_w.filter = self._filter
        agents_w.set_agents(snap.agents)
        self.query_one(WindowPane).show_agent(agents_w.selected(), self._win_index)
        exclude = frozenset(self.config.active_exclude)
        active = pick_active_now(
            snap.agents,
            follow_lock_id=self.follow_lock_id,
            exclude_screens=exclude,
        )
        self.query_one(ActiveNowPane).show(
            active, locked=self.follow_lock_id is not None
        )
        self.query_one(EventStrip).show(snap)

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

    def _sync_windows(self) -> None:
        if self._snap is None:
            return
        agents_w = self.query_one(AgentList)
        self.query_one(WindowPane).show_agent(agents_w.selected(), self._win_index)

    def action_expand(self) -> None:
        pane = self.query_one(WindowPane)
        pane.expanded = True
        self._sync_windows()
        if "narrow" in self.classes:
            self._narrow_page = "windows"
            self._apply_breakpoint()

    def action_collapse(self) -> None:
        pane = self.query_one(WindowPane)
        pane.expanded = False
        self._sync_windows()

    def action_cycle(self) -> None:
        if "narrow" in self.classes:
            order = ["agents", "windows", "active"]
            i = order.index(self._narrow_page)
            self._narrow_page = order[(i + 1) % len(order)]
            self._apply_breakpoint()
            return
        # wide: rotate focus
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
        if "narrow" in self.classes:
            self._narrow_page = "active"
            self._apply_breakpoint()
        self.query_one(ActiveNowPane).focus()

    def action_help(self) -> None:
        self.notify(
            "jk/↑↓ agents · Enter expand · Esc collapse · Tab cycle · "
            "f follow-lock · a Active Now · / filter · q quit",
            title="stop",
        )

    def action_filter(self) -> None:
        self.notify(
            "Filter via ~/.config/stop/config.toml hide_agents for now",
            title="filter",
        )


def run_app(*, fixture: str | None = None, config_path: str | None = None) -> None:
    cfg = load_config(Path(config_path) if config_path else None)
    if fixture:
        host: HostBackend = FixtureHost(Path(fixture))
    else:
        host = LiveHost()
    StopApp(host, config=cfg).run()
