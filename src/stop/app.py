"""Textual TUI for stop."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, RichLog, Static

from .activity import pick_active_now
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
    def show_agent(self, agent: Agent | None, snap: HostSnapshot | None = None) -> None:
        if agent is None:
            self.update("[b]windows[/b]\n(select an agent)")
            return
        lines = [f"[b]{agent.name} windows[/b]"]
        for w in agent.windows:
            style = _state_style(w.state)
            badge = badge_label(w.state)
            pid = f" pid={w.last_seen_pid}" if w.last_seen_pid else ""
            lines.append(f"  [{style}]{w.label}[/{style}] {badge}{pid}")
            preview = (w.last_scrollback or "").strip().splitlines()
            if is_failure(w.state):
                lines.append("    [red]waiting for supervisor…[/red]")
            for pl in preview[-4:]:
                lines.append(f"    {pl[:100]}")
        self.update("\n".join(lines))


class ActiveNowPane(Static):
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
        for pl in preview[-8:]:
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
    #agents { width: 28; border: solid $accent; padding: 0 1; }
    #windows { width: 1fr; border: solid $primary; padding: 0 1; }
    #active { height: 12; border: solid $warning; padding: 0 1; }
    """
    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("j,down", "down", "down", show=False),
        Binding("k,up", "up", "up", show=False),
        Binding("a", "focus_active", "active"),
        Binding("f", "toggle_follow", "follow"),
        Binding("question_mark", "help", "help"),
        Binding("slash", "filter", "filter"),
    ]

    def __init__(self, host: HostBackend):
        super().__init__()
        self.host = host
        self.follow_lock_id: str | None = None
        self._filter_mode = False
        self._filter = ""
        self._snap: HostSnapshot | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield HostStrip(id="host")
        with Vertical(id="body"):
            with Horizontal():
                yield AgentList(id="agents")
                yield WindowPane(id="windows")
            yield ActiveNowPane(id="active")
        yield EventStrip(id="events")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1.0, self.refresh_host)
        self.refresh_host()
        self.query_one(AgentList).focus()

    def refresh_host(self) -> None:
        snap = self.host.snapshot()
        self._snap = snap
        self.query_one(HostStrip).show(snap)
        agents_w = self.query_one(AgentList)
        agents_w.filter = self._filter
        agents_w.set_agents(snap.agents)
        self.query_one(WindowPane).show_agent(agents_w.selected(), snap)
        active = pick_active_now(snap.agents, follow_lock_id=self.follow_lock_id)
        self.query_one(ActiveNowPane).show(active, locked=self.follow_lock_id is not None)
        self.query_one(EventStrip).show(snap)

    def action_down(self) -> None:
        self.query_one(AgentList).move(1)
        self._sync_windows()

    def action_up(self) -> None:
        self.query_one(AgentList).move(-1)
        self._sync_windows()

    def _sync_windows(self) -> None:
        if self._snap is None:
            return
        agents_w = self.query_one(AgentList)
        self.query_one(WindowPane).show_agent(agents_w.selected(), self._snap)

    def action_toggle_follow(self) -> None:
        if self._snap is None:
            return
        if self.follow_lock_id is not None:
            self.follow_lock_id = None
        else:
            active = pick_active_now(self._snap.agents)
            self.follow_lock_id = active.id if active else None
        self.refresh_host()

    def action_focus_active(self) -> None:
        self.query_one(ActiveNowPane).focus()

    def action_help(self) -> None:
        self.notify(
            "jk/↑↓ agents · f follow-lock · a Active Now · / filter · q quit",
            title="stop",
        )

    def action_filter(self) -> None:
        self.notify("Filter: type then Enter (Esc clears) — stub; use config later", title="filter")


def run_app(*, fixture: str | None = None) -> None:
    if fixture:
        host: HostBackend = FixtureHost(Path(fixture))
    else:
        host = LiveHost()
    StopApp(host).run()
