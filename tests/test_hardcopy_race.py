"""Regression: hardcopy truncate must not wipe a good scrollback cache."""

from __future__ import annotations

import os
from pathlib import Path

from stop.host import LiveHost


def test_read_scrollback_keeps_cache_on_empty_file(tmp_path, monkeypatch):
    host = LiveHost(home=tmp_path)
    host.tmp = tmp_path / "stop-tmp"
    host.tmp.mkdir()
    screen = "broca-athena"
    good = "line1\nline2\nline3\n" + "\n".join(f"x{i}" for i in range(50))
    host._scroll_cache[screen] = (0.0, good)

    def fake_run(cmd, **kwargs):
        # Mimic screen: create the unique .hc path empty (truncate race).
        out = Path(cmd[-1])
        out.write_text("")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("stop.host.subprocess.run", fake_run)
    monkeypatch.setattr("stop.host.time.sleep", lambda _s: None)
    got = host.read_scrollback(screen)
    assert got == good
