"""Active Now ranking — prefer human/agent dialogue, ignore bridge noise."""

from __future__ import annotations

import re
from datetime import datetime

from .livelog import unwrap_screen_hardcopy
from .models import EXCLUDED_SCREEN_NAMES, Agent, Window, WindowState

# Infrastructure chatter that must not steal Active Now.
_NOISE_RES = [
    re.compile(p, re.I)
    for p in (
        r"Wrote Otto bridge response file",
        r"otto_bridge/(?:inbox|outbox)/",
        r"broca/run/otto_bridge",
        # Empty inbox poll. Each Broca phrases this differently
        # ("web chat API" vs "partner-bridge"); only a non-zero retrieve counts.
        r"Retrieved 0\b.*\bmessages\b",
        # Idle Letta polls and core-block plumbing. POST /messages is the turn.
        r"HTTP Request: (?:GET|PATCH) .*(?:localhost|127\.0\.0\.1):8284",
        r"plugins\.\w+_vernal_webchat\.api_client",
        r"runtime\.core\.queue:.*attached core block",
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
_LOG_TIMESTAMP_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\]"
)


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
    lines = [ln for ln in unwrap_screen_hardcopy(text) if ln.strip()][-lookback:]
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
    old_lines = unwrap_screen_hardcopy(old)
    new_lines = unwrap_screen_hardcopy(new)
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


def latest_meaningful_log_epoch(text: str) -> float:
    """Timestamp of the newest non-noise log record in screen hardcopy."""
    for line in reversed(unwrap_screen_hardcopy(text)):
        match = _LOG_TIMESTAMP_RE.match(line)
        if not match or classify_line(line) == KIND_NOISE:
            continue
        try:
            return datetime.strptime(
                match.group(1), "%Y-%m-%d %H:%M:%S"
            ).timestamp()
        except ValueError:
            continue
    return 0.0


def next_activity_epoch(
    previous_epoch: float,
    old_scrollback: str,
    new_scrollback: str,
    *,
    now: float,
) -> float:
    """Advance and persist meaningful activity across immutable snapshots."""
    if (old_scrollback or "").strip() == (new_scrollback or "").strip():
        return previous_epoch
    parsed = latest_meaningful_log_epoch(new_scrollback)
    if parsed:
        return max(previous_epoch, parsed)
    if not scrollback_delta_is_noise_only(old_scrollback, new_scrollback):
        return max(previous_epoch, now)
    return previous_epoch


def rank_active_windows(
    agents: list[Agent],
    *,
    exclude_screens: frozenset[str] | None = None,
) -> list[Window]:
    """Windows sorted for Active Now: class → meaningful time → hits → id.

    Dialogue beats infrastructure signals, which beat noise. Within a class,
    the most recent *meaningful* ``last_activity_epoch`` wins. Reconstructing
    hardcopy records before classification keeps split ``Coalesced``/``inbound``
    lines in the dialogue class. Stable ``window.id`` breaks remaining ties.
    """
    exclude = exclude_screens if exclude_screens is not None else EXCLUDED_SCREEN_NAMES
    candidates: list[Window] = []
    for agent in agents:
        for w in agent.windows:
            if w.screen_name and w.screen_name in exclude:
                continue
            if w.state == WindowState.UNMANAGED and w.last_activity_epoch <= 0:
                continue
            # Cron/run log panes are not Active Now targets.
            if not w.screen_name:
                continue
            if not (w.screen_name or "").startswith("broca-"):
                continue
            candidates.append(w)

    def _key(w: Window) -> tuple:
        kind, hits, _ = classify_scrollback(w.last_scrollback)
        # Noise-only windows sort to the bottom even if their raw capture is newest.
        epoch = w.last_activity_epoch if kind >= KIND_OTHER else 0.0
        # Ascending on negated primaries + id → deterministic, no flip-flops.
        return (-kind, -epoch, -hits, w.id)

    candidates.sort(key=_key)
    return candidates


def follow_lock_still_present(
    agents: list[Agent], follow_lock_id: str | None
) -> bool:
    """True when the locked window id still exists in the inventory."""
    if not follow_lock_id:
        return False
    for agent in agents:
        for w in agent.windows:
            if w.id == follow_lock_id:
                return True
    return False


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
        # Also honor lock if the window exists but was filtered from ranked
        # (e.g. empty scrollback) — operator explicitly locked it.
        for agent in agents:
            for w in agent.windows:
                if w.id == follow_lock_id:
                    return w
        # Lock target gone — fall through to auto (caller clears the lock).
    if not ranked:
        return None
    # Prefer dialogue/other signal; if everything is noise, still show the top
    # (stable — noise epochs are zeroed in the sort key, so no flip-flopping).
    # The UI labels it "quiet" so Mark knows it's bridge chatter, not dialogue.
    for w in ranked:
        k, _, _ = classify_scrollback(w.last_scrollback)
        if k >= KIND_OTHER:
            return w
    for w in ranked:
        if (w.last_scrollback or "").strip():
            return w
    return ranked[0]
