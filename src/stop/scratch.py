"""Bounded append-only scratch files under /tmp/stop-<uid>/."""

from __future__ import annotations

import os
import time
from pathlib import Path

from .metrics import metrics_dir

DEFAULT_MAX_BYTES = 256 * 1024
# Unique hardcopy temps (`*.hc`) are unlinked in finally. A crash or a stuck
# screen fd leaves them behind — sweep so /tmp/stop-<uid>/ cannot grow forever.
HC_MAX_AGE_S = 60.0
_PRUNE_SUFFIXES = (".hc", ".json.tmp", ".txt.tmp")
_KEEP_NAMES = frozenset(
    {
        "crontab.txt",
        "metrics.jsonl",
        "trace.json",
        "tui-errors.log",
    }
)


def append_bounded(
    path: Path,
    text: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    mode: int = 0o600,
) -> None:
    """Append ``text`` then trim the file from the front if it exceeds ``max_bytes``."""
    path.parent.mkdir(mode=0o700, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text)
        try:
            os.fchmod(f.fileno(), mode)
        except OSError:
            pass
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    # Keep the tail. Read last max_bytes and rewrite.
    try:
        with path.open("rb") as f:
            f.seek(max(0, size - max_bytes))
            # Drop a partial first line so we stay on a line boundary.
            data = f.read()
        nl = data.find(b"\n")
        if nl >= 0 and nl + 1 < len(data):
            data = data[nl + 1 :]
        path.write_bytes(data)
        os.chmod(path, mode)
    except OSError:
        pass


def tui_errors_path(uid: int | None = None) -> Path:
    return metrics_dir(uid) / "tui-errors.log"


def prune_scratch_dir(
    root: Path,
    *,
    now: float | None = None,
    hc_max_age_s: float = HC_MAX_AGE_S,
) -> int:
    """Delete leftover hardcopy / atomic-write temps. Bounded files stay.

    Returns the number of files removed. Missing dirs and races are ignored.
    """
    when = time.time() if now is None else now
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for path in entries:
        try:
            if not path.is_file():
                continue
            name = path.name
            if name in _KEEP_NAMES:
                continue
            if not name.endswith(_PRUNE_SUFFIXES):
                continue
            age = when - path.stat().st_mtime
            if age <= hc_max_age_s:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return removed
