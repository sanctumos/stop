"""Race-safety for turn worker generation / merge (#4066)."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from stop.turn_stream import (
    TurnStreamState,
    TurnStreamWorker,
    merge_turn_bodies,
    ordered_unique_message_text,
    prune_seen_runs,
)


def _live(epoch: float | None = None) -> str:
    epoch = epoch if epoch is not None else time.time()
    ts = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    return f"[{ts}] INFO Processing message in LIVE mode\n"


def test_merge_turn_bodies_monotonic():
    assert merge_turn_bodies("a", "a\nb") == "a\nb"
    assert merge_turn_bodies("a\nb\nc", "a\nb") == "a\nb\nc"
    # Shorter unrelated must not win.
    assert merge_turn_bodies("long complete answer here", "no") == (
        "long complete answer here"
    )
    assert merge_turn_bodies("hello", "hello") == "hello"


def test_ordered_unique_dedupes_by_id():
    rows = [
        {"id": "1", "message_type": "assistant_message", "content": "hi"},
        {"id": "1", "message_type": "assistant_message", "content": "hi"},
        {"id": "2", "message_type": "assistant_message", "content": "there"},
    ]
    text = ordered_unique_message_text(rows)
    assert text == "hi\n\nthere"


def test_ordered_unique_keeps_reasoning_and_assistant_with_same_id():
    """Letta reuses a step message id across record types."""
    rows = [
        {
            "id": "same",
            "message_type": "reasoning_message",
            "reasoning": "thinking",
        },
        {
            "id": "same",
            "message_type": "assistant_message",
            "content": "FINAL",
        },
    ]
    text = ordered_unique_message_text(rows)
    assert "[think] thinking" in text
    assert text.endswith("FINAL")


def test_prune_seen_runs_keeps_newest():
    order = [f"r{i}" for i in range(12)]
    kept, s = prune_seen_runs(order, maxlen=5)
    assert kept == ["r7", "r8", "r9", "r10", "r11"]
    assert s == set(kept)


def test_disable_during_seek_does_not_linger(tmp_path: Path):
    w = TurnStreamWorker(agents_root=tmp_path)
    gen_holder: dict[str, int] = {}

    def fake_seek(creds, since, gen, **_kwargs):  # noqa: ANN001
        gen_holder["g"] = gen
        # Simulate long seek — disable mid-flight.
        time.sleep(0.15)
        # Stale completion attempt (would have lingered before #4066).
        with w._lock:
            if gen != w._generation or not w.state.enabled:
                return
            w.state.status = "linger"
            w.state.lingering = True
            w.state.text = "SHOULD_NOT_APPEAR"
            w.state.linger_until = time.time() + 60

    with patch.object(w, "_seek_and_stream", fake_seek):
        # Seed creds so seek starts.
        broca = tmp_path / "athena" / "broca"
        broca.mkdir(parents=True)
        (broca / ".env").write_text(
            "AGENT_ID=agent-x\nAGENT_API_KEY=k\nAGENT_ENDPOINT=http://127.0.0.1:9\n",
            encoding="utf-8",
        )
        now = time.time()
        w.tick(selected_agent="athena", broca_scrollback="idle\n", now=now)
        w.tick(
            selected_agent="athena",
            broca_scrollback="idle\n" + _live(now),
            now=now,
        )
        assert w.snapshot().status == "seeking"
        time.sleep(0.05)
        w.set_enabled(False)
        time.sleep(0.25)
    snap = w.snapshot()
    assert snap.enabled is False
    assert snap.active is False
    assert snap.status == "off"
    assert snap.text == ""
    assert "SHOULD_NOT_APPEAR" not in snap.text


def test_stale_generation_apply_ignored(tmp_path: Path):
    w = TurnStreamWorker(agents_root=tmp_path)
    with w._lock:
        w.state.enabled = True
        w.state.text = "live text"
        w.state.status = "streaming"
        w.state.active = True
        stale = w._generation
        w._generation = stale + 1  # supersede
    # Direct call into consume helpers via gen gate.
    assert w._gen_ok(stale) is False
    assert w._gen_ok(stale + 1) is True


def test_immediate_off_on(tmp_path: Path):
    w = TurnStreamWorker(agents_root=tmp_path)
    assert w.toggle() is False
    assert w.snapshot().status == "off"
    assert w.toggle() is True
    assert w.snapshot().enabled is True
    assert w.snapshot().status == "idle"


def test_shorter_corrected_payload_rejected(tmp_path: Path):
    w = TurnStreamWorker(agents_root=tmp_path)
    with w._lock:
        w.state.enabled = True
        w.state.query = "q"
        w.state.text = "> q\n\nfull assistant answer that is long"
        w.state.status = "streaming"
        gen = w._generation

    # Simulate poll applying shorter wrong body through merge.
    cur = w.snapshot().text
    merged = merge_turn_bodies(cur, "> q\n\nno")
    assert merged == cur
    assert w._gen_ok(gen)


def test_disable_joins_worker_thread(tmp_path: Path):
    w = TurnStreamWorker(agents_root=tmp_path)
    started = threading.Event()
    release = threading.Event()

    def blocker(*_a, **_k):
        started.set()
        release.wait(2.0)

    with patch.object(w, "_seek_and_stream", blocker):
        broca = tmp_path / "athena" / "broca"
        broca.mkdir(parents=True)
        (broca / ".env").write_text(
            "AGENT_ID=agent-x\nAGENT_API_KEY=k\nAGENT_ENDPOINT=http://127.0.0.1:9\n",
            encoding="utf-8",
        )
        now = time.time()
        w.tick(selected_agent="athena", broca_scrollback="idle\n", now=now)
        w.tick(
            selected_agent="athena",
            broca_scrollback="idle\n" + _live(now),
            now=now,
        )
        assert w.snapshot().status == "seeking"
        assert started.wait(3.0), "seek thread never entered _seek_and_stream"
        w.set_enabled(False)
        # Thread should be joinable / stopped for new work.
        release.set()
        t = w._thread
        if t is not None:
            t.join(timeout=1.0)
            assert not t.is_alive()
