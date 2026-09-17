"""Parsers for crontab and `screen -list` output."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .models import ScreenSession

# * * * * * /home/rizzn/sanctum/agents/athena/start-athena-broca.sh ...
_CRON_START_RE = re.compile(
    r"(?:^|\s)(?:\S+\s+){5}(?P<path>\S*?/sanctum/agents/(?P<agent>[^/\s]+)/start-[^/\s]*broca\.sh)\b"
)
_LETTA_CRON_RE = re.compile(r"(?:^|\s)(?:\S+\s+){5}\S*?start-letta[^/\s]*\.sh\b")
_SMCP_CRON_RE = re.compile(r"(?:^|\s)(?:\S+\s+){5}\S*?start-smcp\.sh\b")

# 	1818.broca-athena	(09/07/2026 06:32:01 PM)	(Detached)
# 	1201.letta	(09/07/2026 06:31:30 PM)	(Detached)
# 	1874.broca-rico	(09/07/2026 06:32:01 PM)	(Dead ???)
_SCREEN_LINE_RE = re.compile(
    r"^\s*(?P<pid>\d+)\.(?P<name>[^\s\t]+)\s+"
    r"\((?P<started>[^)]+)\)\s+"
    r"\((?P<status>Detached|Attached|Dead[^)]*)\)\s*$"
)

_STARTED_FMTS = (
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
)


def parse_screen_started(text: str) -> float:
    """Parse screen's start timestamp → epoch seconds (0 on failure)."""
    s = (text or "").strip()
    if not s:
        return 0.0
    for fmt in _STARTED_FMTS:
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def parse_crontab(text: str) -> dict[str, str]:
    """Return {agent_name: start_script_path} for cron-managed Brocas.

    Also records pseudo-keys '__letta__' and '__smcp__' when those crons exist
    (empty path string if path not captured).
    """
    managed: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _CRON_START_RE.search(line)
        if m:
            managed[m.group("agent")] = m.group("path")
            continue
        if _LETTA_CRON_RE.search(line):
            managed["__letta__"] = ""
        if _SMCP_CRON_RE.search(line):
            managed["__smcp__"] = ""
    return managed


def parse_screen_list(text: str) -> list[ScreenSession]:
    """Parse `screen -ls` / `screen -list` output into sessions."""
    sessions: list[ScreenSession] = []
    for line in text.splitlines():
        m = _SCREEN_LINE_RE.match(line)
        if not m:
            continue
        status = m.group("status")
        # Normalize "Dead ???" → "Dead"
        if status.startswith("Dead"):
            status = "Dead"
        sessions.append(
            ScreenSession(
                name=m.group("name"),
                pid=int(m.group("pid")),
                status=status,
                started_at_epoch=parse_screen_started(m.group("started")),
            )
        )
    return sessions


def discover_agent_dirs(agents_root: Path) -> dict[str, Path]:
    """Map agent name → directory for each child of agents_root that looks like an agent."""
    if not agents_root.is_dir():
        return {}
    out: dict[str, Path] = {}
    for child in sorted(agents_root.iterdir()):
        if not child.is_dir():
            continue
        # Prefer start-*-broca.sh presence; also accept broca/ subdirectory.
        starts = list(child.glob("start-*-broca.sh")) + list(child.glob("start-broca.sh"))
        if starts or (child / "broca").is_dir():
            out[child.name] = child
    return out


def find_start_script(agent_dir: Path) -> Path | None:
    for pat in ("start-*-broca.sh", "start-broca.sh"):
        hits = sorted(agent_dir.glob(pat))
        if hits:
            return hits[0]
    return None
