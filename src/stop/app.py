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
from textual.widgets import Footer, Header, Input, RichLog, Static

from .activity import pick_active_now
from .collector import HostCollector
from .config import StopConfig, apply_agent_order, load_config
from .host import FixtureHost, HostBackend, LiveHost
from .livelog import LiveLogFeed
from .metrics import METRICS
from .models import Agent, HostSnapshot, Window, WindowState
from .state import badge_label, is_failure, short_badge
from .turn_stream import TurnStreamState, TurnStreamWorker


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



def _set_title(widget, title: str) -> None:
    """Set border title only when it changes (avoids chrome flicker)."""
    if getattr(widget, "border_title", None) != title:
        widget.border_title = title

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
            "l            jump to Letta pane\n"
            "t            toggle turn-stream overlay (default ON)\n"
            "f            follow-lock / release Active Now\n"
            "/            filter agents\n"
            "?            this help\n"
            "q            quit\n\n"
            "No restart button — cron restarts agents.\n"
            "Active Now follows human/agent dialogue — not otto_bridge chatter.\n"
            "Bottom row: Active Now (left) + Letta console (right).\n"
            "Turn stream: Broca turn-start → Letta /v1/runs/{id}/stream;\n"
            "  step chunks (not per-token); pane follows the bottom; linger 60s; t toggles.\n"
            "stop screen is never hardcopied into this TUI.\n"
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
    """Host meters: continuum bars + rates (no block-glyph sparklines)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @staticmethod
    def _bar(pct: float, width: int = 14) -> str:
        """Filled █ + empty ─ (shade/block grit reads as diamonds in many fonts)."""
        pct = max(0.0, min(100.0, pct))
        filled = int(round((pct / 100.0) * width))
        return "█" * filled + "─" * (width - filled)

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

        cpu_style = self._pressure_style(snap.cpu_percent)
        ram_style = self._pressure_style(mem_pct)

        line1 = (
            f"[b]CPU[/b] [{cpu_style}]{self._bar(snap.cpu_percent)}[/{cpu_style}] "
            f"{snap.cpu_percent:5.1f}%  "
            f"load [b]{load[0]:.2f}[/b] {load[1]:.2f} {load[2]:.2f}"
        )
        line2 = (
            f"[b]RAM[/b] [{ram_style}]{self._bar(mem_pct)}[/{ram_style}] "
            f"{snap.mem_used_gib:4.1f}/{snap.mem_total_gib:.1f}G {mem_pct:3.0f}%  "
            f"[b]NET[/b] [cyan]↑{self._rate(snap.net_up_bps)}[/cyan] "
            f"[magenta]↓{self._rate(snap.net_down_bps)}[/magenta]"
        )
        _paint(self, line1 + "\n" + line2)


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


class WindowPane(Vertical):
    """Selected agent: compact window list + append-only live log + turn stream."""

    can_focus = True
    expanded = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._feed = LiveLogFeed(limit=400, width=200)
        self._agent: Agent | None = None
        self._win_index = 0
        self._turn_visible = False
        self._turn_body: str | None = None
        self._empty_kind: str | None = None  # none|select|empty — avoid repeat clears

    def compose(self) -> ComposeResult:
        yield Static(id="win-meta")
        yield RichLog(
            id="win-log",
            max_lines=400,
            min_width=20,
            wrap=False,
            highlight=False,
            markup=False,
            auto_scroll=False,
        )
        with Vertical(id="turn-panel"):
            yield Static(id="turn-meta")
            yield RichLog(
                id="turn-log",
                max_lines=400,
                min_width=20,
                wrap=True,
                highlight=False,
                markup=False,
                auto_scroll=True,
            )

    def on_mount(self) -> None:
        self.query_one("#turn-panel").display = False

    @staticmethod
    def _bridge_bit(w: Window | None) -> str:
        if w is None or w.bridge_inbox_count is None:
            return ""
        return f" · bridge in={w.bridge_inbox_count} out={w.bridge_outbox_count or 0}"

    def show_agent(self, agent: Agent | None, win_index: int = 0) -> None:
        self._agent = agent
        self._win_index = win_index
        meta = self.query_one("#win-meta", Static)
        log = self.query_one("#win-log", RichLog)

        if agent is None:
            _set_title(self, "windows")
            _paint(meta, "(select an agent)")
            if self._empty_kind != "select":
                self._feed.reset()
                log.clear()
                self._empty_kind = "select"
            return

        if not agent.windows:
            _set_title(self, f"{agent.name} windows")
            _paint(meta, "(no windows)")
            if self._empty_kind != "empty":
                self._feed.reset()
                log.clear()
                self._empty_kind = "empty"
            return

        self._empty_kind = None
        w = agent.windows[win_index % len(agent.windows)]
        broca = next(
            (x for x in agent.windows if (x.screen_name or "").startswith("broca-")),
            None,
        )
        bit = self._bridge_bit(broca if not self.expanded else w)

        if self.expanded:
            _set_title(self, f"{agent.name} / {w.label}{bit} — Esc to collapse")
            style = _state_style(w.state)
            head = f"[{style}]{badge_label(w.state)}[/{style}]"
            if is_failure(w.state):
                miss = f" — missing {int(w.seconds_missing)}s" if w.seconds_missing else ""
                head += f"\n[red]waiting for supervisor…{miss}[/red]"
            _paint(meta, head)
        else:
            _set_title(self, f"{agent.name} windows{bit} — Enter to expand")
            lines: list[str] = []
            for i, win in enumerate(agent.windows):
                mark = ">" if i == win_index else " "
                style = _state_style(win.state)
                pid = f" pid={win.last_seen_pid}" if win.last_seen_pid else ""
                miss = ""
                if is_failure(win.state) and win.seconds_missing:
                    miss = f" {int(win.seconds_missing)}s"
                lines.append(
                    f"{mark} [{style}]{_plain(win.label)}[/{style}] "
                    f"{badge_label(win.state)}{miss}[dim]{pid}[/dim]"
                )
                if is_failure(win.state):
                    lines.append("    [red]waiting for supervisor…[/red]")
            _paint(meta, "\n".join(lines) if lines else "(none)")

        # Live log = selected window only; append new lines, never rewrite on idle.
        # Do not include expanded in the key — Enter/Esc must not clear the log (#4067).
        source_key = f"{agent.name}:{w.id}"
        self._feed.sync(log, w.last_scrollback, source_key=source_key)

    def show_turn(self, state: TurnStreamState) -> None:
        """Show/hide the turn-stream overlay under the selected log."""
        panel = self.query_one("#turn-panel")
        meta = self.query_one("#turn-meta", Static)
        log = self.query_one("#turn-log", RichLog)
        want = bool(state.enabled and state.active and (state.text or state.error))
        revealing = want and not self._turn_visible
        if want != self._turn_visible:
            panel.display = want
            self._turn_visible = want
            if not want:
                self._turn_body = None
                log.clear()
            # Absolute overlay must not touch #win-log scroll or geometry.
        if not want:
            return
        elapsed = ""
        if state.started_at:
            elapsed = f"{max(0, int(time.time() - state.started_at))}s · "
        if state.status == "linger":
            title = f"turn · {state.agent_name} · linger"
            mode = "linger"
        elif state.status == "seeking":
            title = f"turn · {state.agent_name} · seeking run…"
            mode = "seeking"
        elif state.status == "error":
            title = f"turn · {state.agent_name} · error"
            mode = "error"
        else:
            rid = (state.run_id or "")[-12:]
            title = f"turn · {state.agent_name} · live {rid}"
            mode = "step-stream"
        _set_title(panel, title)
        err = f"\n[error] {state.error}" if state.error else ""
        body = (state.text or "") + err
        nchars = len(body)
        _paint(
            meta,
            f"[b]{mode}[/b]  [dim]{elapsed}{nchars} chars · follows end · t off[/dim]",
        )
        # Cap + plain lines for RichLog (markup=False).
        lines = body.splitlines() or [body]
        if len(lines) > 350:
            lines = ["…"] + lines[-349:]
        key = "\n".join(lines)
        if key == self._turn_body and not revealing:
            # Still nudge scroll on live ticks so the end stays visible.
            if state.status in ("streaming", "linger") and log.size.width > 0:
                try:
                    log.scroll_end(animate=False)
                except Exception:
                    pass
            return
        self._turn_body = key

        def _paint_log() -> None:
            if not self._turn_visible:
                return
            # Read latest body at paint time (reveal may lag a frame).
            latest = self._turn_body or ""
            paint_lines = latest.splitlines() or [latest]
            log.clear()
            for ln in paint_lines:
                cleaned = "".join(
                    ch if ch >= " " or ch in "\t" else "?" for ch in ln
                )
                log.write(cleaned[:500], scroll_end=True)
            try:
                log.scroll_end(animate=False)
            except Exception:
                pass

        if revealing or log.size.width <= 0:
            self.call_after_refresh(_paint_log)
        else:
            _paint_log()


class ActiveNowPane(Vertical):
    """Follow the hottest dialogue console — append-only live log."""

    can_focus = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._feed = LiveLogFeed(limit=200, width=200)
        self._was_idle = False

    def compose(self) -> ComposeResult:
        yield Static(id="active-meta")
        yield RichLog(
            id="active-log",
            max_lines=200,
            min_width=20,
            wrap=False,
            highlight=False,
            markup=False,
            auto_scroll=False,
        )

    def show(self, window: Window | None, locked: bool) -> None:
        from .activity import KIND_DIALOGUE, KIND_OTHER, classify_scrollback

        meta = self.query_one("#active-meta", Static)
        log = self.query_one("#active-log", RichLog)
        lock = " · LOCKED" if locked else ""

        if window is None:
            _set_title(self, f"Active Now{lock}")
            _paint(meta, "[dim](idle — waiting for human/agent dialogue)[/dim]")
            if not self._was_idle:
                self._feed.reset()
                log.clear()
                self._was_idle = True
            return

        self._was_idle = False
        style = _state_style(window.state)
        age = _age_stable(window.last_activity_epoch)
        kind, hits, _ = classify_scrollback(window.last_scrollback)
        if kind == KIND_DIALOGUE:
            kind_bit = f"dialogue x{hits}"
        elif kind == KIND_OTHER:
            kind_bit = "signal"
        else:
            kind_bit = "quiet"
        _set_title(self, f"Active Now — {window.label} · {kind_bit} · {age}{lock}")
        _paint(meta, f"[{style}]{badge_label(window.state)}[/{style}]")
        self._feed.sync(log, window.last_scrollback, source_key=window.id)


class LettaPane(Vertical):
    """Dedicated live view of the letta screen session."""

    can_focus = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._feed = LiveLogFeed(limit=200, width=200)
        self._was_missing = False

    def compose(self) -> ComposeResult:
        yield Static(id="letta-meta")
        yield RichLog(
            id="letta-log",
            max_lines=200,
            min_width=20,
            wrap=False,
            highlight=False,
            markup=False,
            auto_scroll=False,
        )

    def show(self, window: Window | None) -> None:
        meta = self.query_one("#letta-meta", Static)
        log = self.query_one("#letta-log", RichLog)
        if window is None:
            _set_title(self, "letta")
            _paint(meta, "[dim](letta screen not running)[/dim]")
            if not self._was_missing:
                self._feed.reset()
                log.clear()
                self._was_missing = True
            return
        self._was_missing = False
        style = _state_style(window.state)
        up = _age(window.started_at_epoch)
        pid = f" pid={window.last_seen_pid}" if window.last_seen_pid else ""
        _set_title(self, f"letta · {badge_label(window.state)} · up {up}{pid}")
        _paint(meta, f"[{style}]{badge_label(window.state)}[/{style}]")
        self._feed.sync(log, window.last_scrollback, source_key="letta")


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
        border: solid $accent; border-title-align: left;
        padding: 0 1;
    }
    #win-meta { height: auto; max-height: 8; layer: base; }
    #win-log { height: 1fr; background: transparent; layer: base; }
    /* True overlay: turn layer paints over the log without stealing its height.
       Prior dock:bottom reflowed #win-log (25→14→25 at 160x45) — idle "reset". */
    #windows {
        width: 1fr; height: 100%;
        border: solid $primary;
        padding: 0 1;
        layout: vertical;
        layers: base turn;
    }
    #turn-panel {
        layer: turn;
        position: absolute;
        offset: 0 55%;
        width: 100%;
        height: 45%;
        max-height: 45%;
        border: solid $success;
        padding: 0 1;
        background: $surface;
    }
    #turn-meta { height: 1; }
    #turn-log { height: 1fr; max-height: 100%; min-height: 5; background: transparent; }
    #active {
        width: 1fr; height: 100%;
        border: solid $warning;
        padding: 0 1;
    }
    #active-meta { height: auto; max-height: 2; }
    #active-log { height: 1fr; background: transparent; }
    #bottom { height: 1fr; min-height: 12; }
    #letta {
        width: 1fr; height: 100%;
        border: solid $secondary;
        padding: 0 1;
    }
    #letta-meta { height: auto; max-height: 2; }
    #letta-log { height: 1fr; background: transparent; }
    #agents:focus, #windows:focus, #active:focus, #letta:focus {
        border: heavy $success;
    }
    #filter { dock: bottom; display: none; height: 3; }
    #filter.visible { display: block; }

    HelpScreen { align: center middle; background: $background 80%; }
    #help-body {
        width: 64;
        height: auto;
        max-height: 90%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }

    Screen.medium #agents { width: 26; }

    Screen.narrow #row { layout: vertical; }
    Screen.narrow #bottom { layout: vertical; }
    Screen.narrow #agents { width: 1fr; height: 1fr; }
    Screen.narrow #windows { width: 1fr; height: 1fr; display: none; }
    Screen.narrow #bottom { height: 1fr; display: none; }
    Screen.narrow.page-windows #agents { display: none; }
    Screen.narrow.page-windows #windows { display: block; }
    Screen.narrow.page-active #agents { display: none; }
    Screen.narrow.page-active #bottom { display: block; height: 1fr; }
    Screen.narrow.page-active #letta { display: none; }
    Screen.narrow.page-letta #agents { display: none; }
    Screen.narrow.page-letta #bottom { display: block; height: 1fr; }
    Screen.narrow.page-letta #active { display: none; }

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
        Binding("l", "focus_letta", "letta"),
        Binding("t", "toggle_turn_stream", "turns"),
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
        self._painted_rev = -1
        self._last_collect_error = ""
        self._turn: TurnStreamWorker | None = None
        self._collector = HostCollector(host, on_update=self._on_collector_update)
        if isinstance(self.host, LiveHost):
            self.host.hardcopy_interval_s = float(self.config.refresh_hardcopy_s)
            self._turn = TurnStreamWorker(
                agents_root=self.host.agents_root,
                on_update=self._on_turn_update,
            )

    def _on_collector_update(self) -> None:
        try:
            self.call_from_thread(self._paint_from_collector)
        except Exception:
            # App may not be running yet / already exiting.
            pass

    def _on_turn_update(self) -> None:
        """Worker thread → UI thread: repaint turn pane immediately."""
        try:
            self.call_from_thread(self._paint_turn_from_worker)
        except Exception:
            pass

    def _paint_turn_from_worker(self) -> None:
        if self._turn is None:
            return
        try:
            self.query_one(WindowPane).show_turn(self._turn.snapshot())
        except Exception:
            pass

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield HostStrip(id="host")
        with Vertical(id="body"):
            with Horizontal(id="row"):
                yield AgentList(id="agents")
                yield WindowPane(id="windows")
            with Horizontal(id="bottom"):
                yield ActiveNowPane(id="active")
                yield LettaPane(id="letta")
        yield EventStrip(id="events")
        yield Input(placeholder="filter agents… (Enter apply, Esc cancel)", id="filter")
        yield Footer()

    def on_mount(self) -> None:
        import threading

        # Mark the Textual thread so accidental LiveHost.snapshot() from here is counted.
        if isinstance(self.host, LiveHost):
            self.host.ui_thread_ident = threading.get_ident()
        self._collector.start()
        interval = max(1.0, float(self.config.refresh_host_s))
        self.set_interval(interval, self.refresh_host)
        self.refresh_host()
        self.query_one(AgentList).focus()
        self._apply_breakpoint()

    def on_unmount(self) -> None:
        self._collector.stop(timeout=2.0)

    def on_resize(self, event) -> None:  # noqa: ANN001
        self._apply_breakpoint()

    def _apply_breakpoint(self) -> None:
        size = self.size
        screen = self.screen
        screen.remove_class(
            "medium", "narrow", "tiny", "page-windows", "page-active", "page-letta"
        )
        if size.height < 24:
            screen.add_class("tiny")
        if size.width < 80:
            screen.add_class("narrow")
            if self._narrow_page == "windows":
                screen.add_class("page-windows")
            elif self._narrow_page == "active":
                screen.add_class("page-active")
            elif self._narrow_page == "letta":
                screen.add_class("page-letta")
        elif size.width < 120:
            screen.add_class("medium")

    @staticmethod
    def _letta_window(snap: HostSnapshot) -> Window | None:
        for agent in snap.agents:
            if agent.name != "System":
                continue
            for w in agent.windows:
                if w.screen_name == "letta":
                    return w
        return None

    def _update_focus_screens(self, selected: Agent | None, active: Window | None) -> None:
        if not isinstance(self.host, LiveHost):
            return
        # Ordered priority: selected Broca → Active Now → Letta → selected secondary.
        ordered: list[str] = []
        if selected:
            for w in selected.windows:
                if w.screen_name and (w.screen_name or "").startswith("broca-"):
                    ordered.append(w.screen_name)
                    break
        if active and active.screen_name:
            ordered.append(active.screen_name)
        ordered.append("letta")
        if selected:
            for w in selected.windows:
                if (
                    w.screen_name
                    and w.screen_name not in ordered
                    and w.screen_name not in ("stop",)
                ):
                    ordered.append(w.screen_name)
        self.host.set_focus_screens(ordered)

    def refresh_host(self) -> None:
        """UI tick: request background collect + paint newest completed revision."""
        # Focus screens from last paint before waking the worker.
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
        except Exception:
            pass
        self._collector.request()
        self._paint_from_collector()

    def _paint_from_collector(self) -> None:
        try:
            rev, snap, err = self._collector.latest()
            if snap is None:
                if err and err != self._last_collect_error:
                    self._last_collect_error = err
                    self._errors += 1
                    self.query_one(EventStrip).update(
                        f"[red]refresh error ({self._errors}): {_plain(err[:80])}[/red]"
                    )
                return
            if rev == self._painted_rev:
                if err and err != self._last_collect_error:
                    self._last_collect_error = err
                    self.query_one(EventStrip).update(
                        f"[red]refresh error: {_plain(err[:80])}[/red]"
                    )
                return
            self._painted_rev = rev
            self._last_collect_error = err
            snap.agents = apply_agent_order(snap.agents, self.config)
            self._snap = snap
            self.query_one(HostStrip).show(snap)
            agents_w = self.query_one(AgentList)
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
            self.query_one(LettaPane).show(self._letta_window(snap))
            self._tick_turn_stream(selected)
            if err:
                self.query_one(EventStrip).update(
                    f"[red]refresh error: {_plain(err[:80])}[/red]"
                )
            else:
                self.query_one(EventStrip).show(snap)
            self._errors = 0
            METRICS.bump_render_revision()
            if METRICS.snapshot_count and METRICS.snapshot_count % 30 == 0:
                try:
                    METRICS.flush()
                except OSError:
                    pass
        except Exception as exc:  # noqa: BLE001 — keep TUI alive
            self._errors += 1
            METRICS.note_refresh_error()
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

    def _tick_turn_stream(self, selected: Agent | None) -> None:
        if self._turn is None:
            return
        broca_text = ""
        name = selected.name if selected else None
        if selected:
            for w in selected.windows:
                if (w.screen_name or "").startswith("broca-"):
                    broca_text = w.last_scrollback or ""
                    break
        self._turn.tick(selected_agent=name, broca_scrollback=broca_text)
        self.query_one(WindowPane).show_turn(self._turn.snapshot())

    def action_toggle_turn_stream(self) -> None:
        if self._turn is None:
            return
        on = self._turn.toggle()
        # Immediate UI feedback in the event strip.
        self.query_one(EventStrip).update(
            f"events: turn-stream {'ON' if on else 'OFF'} (t to toggle)"
        )
        self.query_one(WindowPane).show_turn(self._turn.snapshot())

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
            order = ["agents", "windows", "active", "letta"]
            i = order.index(self._narrow_page) if self._narrow_page in order else 0
            self._narrow_page = order[(i + 1) % len(order)]
            self._apply_breakpoint()
            return
        focused = self.focused
        ids = ("agents", "windows", "active", "letta")
        widgets = {
            "agents": self.query_one(AgentList),
            "windows": self.query_one(WindowPane),
            "active": self.query_one(ActiveNowPane),
            "letta": self.query_one(LettaPane),
        }
        current = None
        node = focused
        while node is not None:
            nid = getattr(node, "id", None)
            if nid in ids:
                current = nid
                break
            node = node.parent
        if current is None:
            widgets["agents"].focus()
            return
        nxt = ids[(ids.index(current) + 1) % len(ids)]
        widgets[nxt].focus()

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

    def action_focus_letta(self) -> None:
        if "narrow" in self.screen.classes:
            self._narrow_page = "letta"
            self._apply_breakpoint()
        self.query_one(LettaPane).focus()

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
