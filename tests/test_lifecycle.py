"""Full seek→stream→linger→disable contract without LiveHost (#4452)."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from stop.turn_stream import LettaAgentCreds, TurnStreamWorker


def _live(epoch: float) -> str:
    ts = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    return f"[{ts}] INFO Processing message in LIVE mode\n"


def test_seek_stream_linger_disable_lifecycle(tmp_path: Path, monkeypatch):
    broca = tmp_path / "athena" / "broca"
    broca.mkdir(parents=True)
    (broca / ".env").write_text(
        "AGENT_ID=agent-x\nAGENT_API_KEY=k\nAGENT_ENDPOINT=http://127.0.0.1:9\n",
        encoding="utf-8",
    )
    w = TurnStreamWorker(agents_root=tmp_path)
    w.LINGER_S = 2.0
    w.SSE_READ_TIMEOUT_S = 1.0

    monkeypatch.setattr(
        "stop.turn_stream.list_runs_for_agent",
        lambda *a, **k: [
            {
                "id": "run-abc",
                "status": "running",
                "created_at": time.time(),
            }
        ],
    )
    monkeypatch.setattr(
        "stop.turn_stream.pick_run_id",
        lambda *a, **k: "run-abc",
    )
    monkeypatch.setattr(
        "stop.turn_stream.fetch_run_status",
        lambda *a, **k: "completed",
    )
    monkeypatch.setattr(
        "stop.turn_stream.fetch_run_messages",
        lambda *a, **k: [
            {
                "id": "m1",
                "message_type": "assistant_message",
                "content": "hello from lifecycle",
            }
        ],
    )

    class FakeResp:
        def readline(self):
            return b""

        def close(self):
            return None

    monkeypatch.setattr(
        "stop.turn_stream._safe_urlopen",
        lambda *a, **k: FakeResp(),
    )

    now = time.time()
    w.tick(selected_agent="athena", broca_scrollback="idle\n", now=now)
    w.tick(
        selected_agent="athena",
        broca_scrollback="idle\n" + _live(now),
        now=now,
    )
    assert w.snapshot().status == "seeking"

    # Wait for seek thread to finish stream → linger.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        snap = w.snapshot()
        if snap.status == "linger" and "hello from lifecycle" in (snap.text or ""):
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"never lingered: {w.snapshot()}")

    w.set_enabled(False)
    snap = w.snapshot()
    assert snap.status == "off"
    assert snap.active is False
    assert snap.text == ""
    t = w._thread
    if t is not None and t.is_alive():
        t.join(timeout=2.0)
        assert not t.is_alive()


def test_unmount_shutdown_leaves_no_live_callback(tmp_path: Path):
    """App quit must clear on_update so call_from_thread cannot fire after unmount."""
    fired = {"n": 0}

    def boom():
        fired["n"] += 1
        raise RuntimeError("should not paint after shutdown")

    w = TurnStreamWorker(agents_root=tmp_path, on_update=boom)
    w.shutdown(timeout=1.0)
    w._notify()  # must no-op
    assert fired["n"] == 0
    assert w.on_update is None
