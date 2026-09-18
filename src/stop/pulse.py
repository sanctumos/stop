"""Per-agent activity sparklines, normalized to each agent's own volume."""

from __future__ import annotations

import math
from collections import Counter, deque

from .activity import KIND_NOISE, classify_line
from .livelog import unwrap_screen_hardcopy
from .models import Agent

GLYPHS = "▁▂▃▄▅▆▇█"


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


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * p
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    fraction = index - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


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
        self._floor: dict[str, float] = {}
        self._excess: dict[str, deque[float]] = {}
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
        seen = self._seen_at.get(name, now)
        self._text[name] = text
        self._seen_at[name] = now
        if previous is None:
            self._floor.setdefault(name, 0.0)
            return
        elapsed = max(0.25, now - seen)
        rate = new_meaningful_lines(previous, text) / elapsed
        floor = self._floor.get(name, 0.0)
        # Fall quickly, rise slowly: a steady polling floor must not become a burst.
        alpha = 0.35 if rate <= floor else 0.03
        floor = (1.0 - alpha) * floor + alpha * rate
        self._floor[name] = floor
        excess = max(0.0, rate - floor)
        samples = self._excess.setdefault(name, deque(maxlen=self._history))
        samples.append(excess)
        scale = _percentile(list(samples), 0.9)
        level = 0.0 if scale <= 1e-9 else min(1.0, excess / scale)
        events = self._events.setdefault(name, deque(maxlen=self._history))
        events.append((now, level))

    def _forget(self, name: str) -> None:
        for store in (
            self._floor,
            self._excess,
            self._events,
            self._text,
            self._seen_at,
        ):
            store.pop(name, None)

    @staticmethod
    def _glyph(level: float) -> str:
        index = int(round(max(0.0, min(1.0, level)) * (len(GLYPHS) - 1)))
        return GLYPHS[index]
