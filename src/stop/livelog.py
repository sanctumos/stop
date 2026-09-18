"""Append-only live log sync — avoid full pane repaints that look like scrolling."""

from __future__ import annotations

import re
import unicodedata

from textual.widgets import RichLog

# Broca/Rich often put a colored emoji (🔵) between `[5` and `INFO]`. In many
# terminal fonts that glyph renders as a diamond and floods every pane.
_LOG_DOT_REPLACEMENTS = str.maketrans(
    {
        "🔵": "|",
        "🟢": "|",
        "🔴": "|",
        "🟡": "|",
        "🟠": "|",
        "🟣": "|",
        "⚪": "|",
        "⚫": "|",
        "◆": "|",
        "◇": "|",
        "♦": "|",
        "♢": "|",
        "●": "|",
        "○": "|",
        "\ufffd": "|",  # hardcopy replacement for emoji
    }
)

_LOG_START_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\]")
_TAIL_WORD_RE = re.compile(r"([A-Za-z0-9]+)$")
_HEAD_WORD_RE = re.compile(r"^([A-Za-z0-9]+)")


def _screen_wrap_separator(previous: str, continuation: str) -> str:
    """Recover spaces that screen drops at a physical line boundary."""
    if not previous or not continuation:
        return ""
    if continuation[0] in "([{*":
        return " "
    if previous[-1] in "/-_=." or continuation[0] in "/-_=.":
        return ""
    tail = _TAIL_WORD_RE.search(previous)
    head = _HEAD_WORD_RE.match(continuation)
    if tail and head and (len(tail.group(1)) <= 4 or len(head.group(1)) <= 3):
        return ""
    return " "


def unwrap_screen_hardcopy(text: str) -> list[str]:
    """Rejoin GNU screen's physical-width wraps into logical log records.

    Broca screens are commonly 80 columns. ``screen -X hardcopy`` inserts real
    newlines every 80 cells, even through words (``LIVE m`` / ``ode``). Once a
    timestamped log record starts, non-timestamp lines are physical
    continuations until the next timestamp or blank separator.
    """
    out: list[str] = []
    current: str | None = None
    for line in (text or "").splitlines():
        if _LOG_START_RE.match(line):
            if current is not None:
                out.append(current)
            current = line
        elif not line:
            if current is not None:
                out.append(current)
                current = None
        elif current is not None:
            current += _screen_wrap_separator(current, line) + line
        else:
            out.append(line)
    if current is not None:
        out.append(current)
    return out


def clean_log_line(line: str, *, width: int = 200) -> str:
    """Strip controls and decorative log dots; no Rich markup (RichLog markup=False)."""
    text = (line or "").translate(_LOG_DOT_REPLACEMENTS)
    out: list[str] = []
    for ch in text:
        if ch >= " " or ch in "\t":
            # Drop leftover emoji / symbol-other that still look like diamonds.
            o = ord(ch)
            if o >= 0x1F300:  # misc emoji blocks
                out.append("|")
                continue
            cat = unicodedata.category(ch)
            if cat == "So" and not (0x2500 <= o <= 0x259F):
                out.append("|")
                continue
            out.append(ch)
        else:
            out.append("?")
    return "".join(out).rstrip()[:width]


def lines_from_scrollback(text: str, *, limit: int = 200, width: int = 200) -> list[str]:
    raw = unwrap_screen_hardcopy(text)
    # Keep trailing empties out of the stable comparison window.
    while raw and not raw[-1].strip():
        raw.pop()
    return [clean_log_line(ln, width=width) for ln in raw[-limit:]]


def diff_log_lines(old: list[str], new: list[str]) -> tuple[str, list[str]]:
    """Decide how to update a live log widget.

    Returns (mode, lines) where mode is:
      noop    — identical; do not touch the widget
      append  — write only the returned lines
      replace — clear + write the returned lines (selection change or rewind)
    """
    if new == old:
        return "noop", []
    if not old:
        return "replace", new
    # Truncated / empty hardcopy mid-write — never wipe a good buffer.
    if old and (not new or (len(old) >= 40 and len(new) < max(10, len(old) // 3))):
        return "noop", []
    if len(new) >= len(old) and new[: len(old)] == old:
        return "append", new[len(old) :]
    # Hardcopy tail slid: find largest overlap of old suffix with new prefix.
    max_o = min(len(old), len(new))
    for o in range(max_o, 0, -1):
        if old[-o:] == new[:o]:
            return "append", new[o:]
    return "replace", new


class LiveLogFeed:
    """Stateful feeder for one RichLog — append-only unless the source resets."""

    def __init__(self, *, limit: int = 200, width: int = 200) -> None:
        self.limit = limit
        self.width = width
        self._source_key: str | None = None
        self._lines: list[str] = []

    def reset(self) -> None:
        self._source_key = None
        self._lines = []

    def sync(self, log: RichLog, text: str, *, source_key: str, follow: bool = True) -> str:
        """Apply scrollback `text` for `source_key`. Returns mode used.

        When ``follow`` is False, append/replace without scrolling to end so
        manual scrollback stays put (#4070).
        """
        new_lines = lines_from_scrollback(text, limit=self.limit, width=self.width)
        if source_key != self._source_key:
            # Don't clear a populated widget into an empty truncated capture.
            if self._lines and not new_lines:
                return "noop"
            self._source_key = source_key
            self._lines = new_lines
            log.clear()
            for ln in new_lines:
                log.write(ln, scroll_end=follow)
            return "replace"
        mode, chunk = diff_log_lines(self._lines, new_lines)
        if mode == "noop":
            return "noop"
        if mode == "append":
            for ln in chunk:
                # Only scroll when following — never on idle ticks or manual scrollback.
                log.write(ln, scroll_end=follow)
            self._lines = new_lines
            return "append"
        log.clear()
        for ln in new_lines:
            log.write(ln, scroll_end=follow)
        self._lines = new_lines
        return "replace"
