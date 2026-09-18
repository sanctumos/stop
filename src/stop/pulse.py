"""Per-agent activity bars.

Empty inbox polls stay dark. A real line lights the bar, and an open Letta
turn stays lit while she is thinking even if the log goes quiet.
"""

from __future__ import annotations

import math
import re
from collections import Counter, deque

from .activity import KIND_NOISE, classify_line
from .livelog import unwrap_screen_hardcopy
from .models import Agent

_TURN_OPEN = re.compile(
    r"Processing message in LIVE mode|Coalesced inbound|Attaching user core block",
    re.I,
)
_TURN_CLOSE = re.compile(
    r"Routing response through|Detaching core block|turn timed out|Stream processing timed out",
    re.I,
)

# tty1 on moya uses the 512-glyph Uni2-Fixed16 console font. It has █ ░ ▒
# and box drawing, and it does not have the eighth-blocks (▁▂▃…). Missing
# glyphs draw as diamonds. These four get darker with volume, same idea as
# the CPU bar, and they are actually in that font.
GLYPHS = "─░▒█"


def activity_text(agent: Agent) -> str:
    """Best recent text for pulse scoring.

    A blank Broca screen is legitimate when stdout is redirected. Fall through
    to that agent's own run log rather than scoring another console.
    """
    broca = next(
        (
            w
            for w in agent.windows
            if (w.screen_name or "").startswith("broca-")
        ),
        None,
    )
    if broca is not None and (broca.last_scrollback or "").strip():
        return broca.last_scrollback
    for window in agent.windows:
        if window.log_path and (window.last_scrollback or "").strip():
            return window.last_scrollback
    return broca.last_scrollback if broca is not None else ""


def new_meaningful_lines(old: str, new: str) -> int:
    """Count newly arrived non-noise logical records."""
    old_lines = [ln for ln in unwrap_screen_hardcopy(old) if ln.strip()]
    new_lines = [ln for ln in unwrap_screen_hardcopy(new) if ln.strip()]
    if new_lines == old_lines:
        return 0
    # Rolling hardcopies drop the head and keep the tail. Count only lines
    # that were not already present, so a one-line advance is not an 8-line burst.
    previous = Counter(old_lines)
    arrived = 0
    for line, count in Counter(new_lines).items():
        extra = count - previous.get(line, 0)
        if extra > 0 and classify_line(line) != KIND_NOISE:
            arrived += extra
    return arrived


def turn_open(text: str) -> bool:
    """True while Broca has started a turn and not yet finished it.

    Letta can think for minutes without writing another log line. The meter
    has to stay up through that silence.
    """
    lines = [ln for ln in unwrap_screen_hardcopy(text) if ln.strip()][-40:]
    open_ = False
    for line in lines:
        if _TURN_CLOSE.search(line):
            open_ = False
        elif _TURN_OPEN.search(line):
            open_ = True
    return open_


class AgentPulse:
    """Bounded relative activity tracker."""

    def __init__(
        self,
        *,
        width: int = 4,
        window_s: float = 30.0,
        history: int = 48,
    ) -> None:
        self.width = width
        self.window_s = window_s
        self._events: dict[str, deque[tuple[float, float]]] = {}
        self._text: dict[str, str] = {}
        self._seen_at: dict[str, float] = {}
        self._history = history

    def observe(self, agents: list[Agent], now: float) -> dict[str, str]:
        names = {agent.name for agent in agents}
        for stale in [name for name in self._text if name not in names]:
            self._forget(stale)
        for agent in agents:
            self._observe_one(agent.name, activity_text(agent), now)
        return {agent.name: self.render(agent.name, now) for agent in agents}

    def render(self, name: str, now: float) -> str:
        slot = self.window_s / self.width
        levels = [0.0] * self.width
        for when, level in self._events.get(name, ()):
            age = now - when
            if age < 0 or age >= self.window_s:
                continue
            index = self.width - 1 - int(age / slot)
            index = max(0, min(self.width - 1, index))
            decayed = level * math.exp(-age / (self.window_s / 2))
            levels[index] = max(levels[index], decayed)
        return "".join(self._glyph(level) for level in levels)

    def _observe_one(self, name: str, text: str, now: float) -> None:
        previous = self._text.get(name)
        self._text[name] = text
        self._seen_at[name] = now
        if previous is None:
            if turn_open(text):
                self._events.setdefault(name, deque(maxlen=self._history)).append((now, 1.0))
            return
        arrived = new_meaningful_lines(previous, text)
        # A real line is a real line. Do not scale it down against this
        # agent's own recent volume — that hid an in-flight Athena turn.
        # An open turn stays lit even when the log goes quiet mid-think.
        level = 1.0 if turn_open(text) or arrived > 0 else 0.0
        events = self._events.setdefault(name, deque(maxlen=self._history))
        events.append((now, level))

    def _forget(self, name: str) -> None:
        for store in (
            self._events,
            self._text,
            self._seen_at,
        ):
            store.pop(name, None)

    @staticmethod
    def _glyph(level: float) -> str:
        index = int(round(max(0.0, min(1.0, level)) * (len(GLYPHS) - 1)))
        return GLYPHS[index]
