"""Active Now ranking — prefer human/agent dialogue, ignore bridge noise."""

from __future__ import annotations

import re

from .models import EXCLUDED_SCREEN_NAMES, Agent, Window, WindowState

# Infrastructure chatter that must not steal Active Now.
_NOISE_RES = [
    re.compile(p, re.I)
    for p in (
        r"Wrote Otto bridge response file",
        r"otto_bridge/(?:inbox|outbox)/",
        r"broca/run/otto_bridge",
        r"Retrieved 0 messages from web chat",
        r"httpx: HTTP Request:.*(?:localhost|127\.0\.0\.1):8284",
        r"plugins\.\w+_vernal_webchat\.api_client",
        r"runtime\.core\.queue:.*attached core block",
        r"HTTP/1\.1 200 OK",
        r"systemctl --user is-active",
    )
]

# Human↔agent / agent↔agent signal — what Mark wants to follow.
_DIALOGUE_RES = [
    re.compile(p, re.I)
    for p in (
        r"\btelegram\b",
        r"\binbound\b",
        r"\boutbound\b",
        r"\bwebchat\b.*\bmessage\b",
        r"\bspeaker\b",
        r"\bmessage_id\b",
        r"\bsend_message\b",
        r"\breply(?:ing|ing)?\b",
        r"\bfrom (?:user|human|mark)\b",
        r"\bhuman\b",
        r"\bdictated by\b",
        r"\bDM\b",
        r"\bchat\b.*\breceived\b",
        r"\breceived (?:a )?message\b",
        r"\boutgoing message\b",
        r"\bincoming message\b",
        r"\bagent-to-agent\b",
        r"\bbroca.*(?:reply|message|inbox)\b",
    )
]

KIND_NOISE = 0
KIND_OTHER = 1
KIND_DIALOGUE = 2


def classify_line(line: str) -> int:
    """Return KIND_* for one log line."""
    s = line.strip()
    if not s:
        return KIND_NOISE
    if any(r.search(s) for r in _NOISE_RES):
        return KIND_NOISE
    if any(r.search(s) for r in _DIALOGUE_RES):
        return KIND_DIALOGUE
    return KIND_OTHER


def classify_scrollback(text: str, *, lookback: int = 40) -> tuple[int, int, str]:
    """Classify recent scrollback.

    Returns (best_kind, dialogue_hits, preview_line).
    best_kind is the strongest kind among the last `lookback` lines (dialogue > other > noise).
    """
    lines = [ln for ln in (text or "").splitlines() if ln.strip()][-lookback:]
    if not lines:
        return KIND_NOISE, 0, ""
    best = KIND_NOISE
    dialogue_hits = 0
    preview = lines[-1]
    # Prefer the newest dialogue line as preview when present.
    for ln in reversed(lines):
        kind = classify_line(ln)
        if kind == KIND_DIALOGUE:
            dialogue_hits += 1
            if best < KIND_DIALOGUE:
                best = KIND_DIALOGUE
                preview = ln
        elif kind == KIND_OTHER and best < KIND_OTHER:
            best = KIND_OTHER
            if best != KIND_DIALOGUE:
                preview = ln
    return best, dialogue_hits, preview


def scrollback_delta_is_noise_only(old: str, new: str) -> bool:
    """True when the new text only added noise (or nothing meaningful)."""
    old_lines = (old or "").splitlines()
    new_lines = (new or "").splitlines()
    if new_lines == old_lines:
        return True
    # Lines present in new but not as a suffix match of old — approximate delta.
    if len(new_lines) >= len(old_lines) and new_lines[: len(old_lines)] == old_lines:
        delta = new_lines[len(old_lines) :]
    else:
        # Hardcopy rewrite — score the newest few lines only.
        delta = new_lines[-8:]
    if not delta:
        return True
    return all(classify_line(ln) == KIND_NOISE for ln in delta if ln.strip())


def rank_active_windows(
    agents: list[Agent],
    *,
    exclude_screens: frozenset[str] | None = None,
) -> list[Window]:
    """Windows sorted for Active Now: dialogue first, then other; noise last."""
    exclude = exclude_screens if exclude_screens is not None else EXCLUDED_SCREEN_NAMES
    candidates: list[Window] = []
    for agent in agents:
        for w in agent.windows:
            if w.screen_name and w.screen_name in exclude:
                continue
            if w.state == WindowState.UNMANAGED and w.last_activity_epoch <= 0:
                continue
            # Cron/run log / bore status panes are not Active Now targets.
            if not w.screen_name:
                continue
            if not (w.screen_name or "").startswith("broca-"):
                continue
            candidates.append(w)

    def _key(w: Window) -> tuple:
        kind, hits, _ = classify_scrollback(w.last_scrollback)
        # Noise-only windows sort to the bottom even if their epoch is newest.
        epoch = w.last_activity_epoch if kind >= KIND_OTHER else 0.0
        is_broca = 1 if (w.screen_name or "").startswith("broca-") else 0
        return (kind, hits, epoch, is_broca)

    candidates.sort(key=_key, reverse=True)
    return candidates


def pick_active_now(
    agents: list[Agent],
    *,
    follow_lock_id: str | None = None,
    exclude_screens: frozenset[str] | None = None,
) -> Window | None:
    """Return the follow-locked window if set, else the hottest eligible window."""
    ranked = rank_active_windows(agents, exclude_screens=exclude_screens)
    if follow_lock_id:
        for w in ranked:
            if w.id == follow_lock_id:
                return w
        # Lock target gone — fall through to auto.
    if not ranked:
        return None
    # If the winner is noise-only and everything is noise, still show hottest broca
    # but prefer idle when nothing has dialogue/other signal.
    top = ranked[0]
    kind, _, _ = classify_scrollback(top.last_scrollback)
    if kind == KIND_NOISE:
        # Fall back to any window with other/dialogue; else None → "(idle)".
        for w in ranked:
            k, _, _ = classify_scrollback(w.last_scrollback)
            if k >= KIND_OTHER:
                return w
        return None
    return top
