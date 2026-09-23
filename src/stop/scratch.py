"""Bounded append-only scratch files under /tmp/stop-<uid>/."""

from __future__ import annotations

import os
from pathlib import Path

from .metrics import metrics_dir

DEFAULT_MAX_BYTES = 256 * 1024


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
