"""Unit tests for Broca→Letta turn-stream worker (no network)."""

from __future__ import annotations

import time
from pathlib import Path

from stop.turn_stream import (
    TurnStreamState,
    TurnStreamWorker,
    format_stream_event,
    load_agent_creds,
    scrollback_signals_turn_start,
)


def test_scrollback_detects_new_turn_line():
    prev = "2026-09-17 INFO idle\n2026-09-17 INFO waiting\n"
    new = prev + "2026-09-17 INFO Processing message in LIVE mode\n"
    assert scrollback_signals_turn_start(prev, new) is True


def test_scrollback_ignores_identical():
    text = "Processing message in LIVE mode\n"
    assert scrollback_signals_turn_start(text, text) is False


def test_scrollback_ignores_noise_delta():
    prev = "line1\n"
    new = prev + "HTTP Request: GET http://127.0.0.1:8871/health\n"
    assert scrollback_signals_turn_start(prev, new) is False


def test_scrollback_ignores_lagging_http_post_ok():
    """POST …/messages 200 is logged when the Letta call finishes — not a new turn."""
    prev = "Processing message in LIVE mode\n"
    new = (
        prev
        + 'HTTP Request: POST http://localhost:8284/v1/agents/agent-x/messages "HTTP/1.1 200 OK"\n'
    )
    assert scrollback_signals_turn_start(prev, new) is False


def test_scrollback_ignores_dequeue_alone():
    prev = "idle\n"
    new = prev + "Atomically dequeued message (Queue ID: abc)\n"
    assert scrollback_signals_turn_start(prev, new) is False


def test_format_assistant_and_think():
    assert "hello" in format_stream_event(
        {"message_type": "assistant_message", "content": "hello"}
    )
    think = format_stream_event(
        {"message_type": "reasoning_message", "reasoning": "hmm"}
    )
    assert think.startswith("[think]")
    assert format_stream_event({"message_type": "ping"}) == ""


def test_load_agent_creds(tmp_path: Path):
    broca = tmp_path / "athena" / "broca"
    broca.mkdir(parents=True)
    (broca / ".env").write_text(
        "AGENT_ID=agent-abc\n"
        "AGENT_API_KEY=sekrit\n"
        "AGENT_ENDPOINT=http://127.0.0.1:8284/v1\n",
        encoding="utf-8",
    )
    creds = load_agent_creds(tmp_path, "athena")
    assert creds is not None
    assert creds.agent_id == "agent-abc"
    assert creds.api_key == "sekrit"
    assert creds.endpoint == "http://127.0.0.1:8284"


def test_worker_default_enabled_and_toggle(tmp_path: Path):
    w = TurnStreamWorker(agents_root=tmp_path)
    assert w.snapshot().enabled is True
    assert w.toggle() is False
    assert w.snapshot().enabled is False
    assert w.snapshot().status == "off"
    assert w.toggle() is True
    assert w.snapshot().enabled is True


def test_worker_first_paint_does_not_fire(tmp_path: Path):
    w = TurnStreamWorker(agents_root=tmp_path)
    text = "Processing message in LIVE mode\n"
    w.tick(selected_agent="athena", broca_scrollback=text)
    snap = w.snapshot()
    assert snap.active is False
    assert snap.status == "idle"


def test_worker_second_tick_with_turn_seeks_without_creds(tmp_path: Path):
    w = TurnStreamWorker(agents_root=tmp_path)
    prev = "idle\n"
    w.tick(selected_agent="athena", broca_scrollback=prev)
    w.tick(
        selected_agent="athena",
        broca_scrollback=prev + "Processing message in LIVE mode\n",
    )
    snap = w.snapshot()
    assert snap.active is True
    assert snap.status == "error"
    assert "no Letta creds" in snap.error


def test_toggle_off_clears_active(tmp_path: Path):
    w = TurnStreamWorker(agents_root=tmp_path)
    prev = "idle\n"
    w.tick(selected_agent="athena", broca_scrollback=prev)
    w.tick(
        selected_agent="athena",
        broca_scrollback=prev + "Processing message in LIVE mode\n",
    )
    assert w.snapshot().active is True
    w.set_enabled(False)
    snap = w.snapshot()
    assert snap.enabled is False
    assert snap.active is False
    assert snap.text == ""


def test_linger_ignores_new_turn_signals(tmp_path: Path):
    """Lagging Broca lines during linger must not wipe the completed turn text."""
    w = TurnStreamWorker(agents_root=tmp_path)
    now = time.time()
    with w._lock:
        w.state = TurnStreamState(
            enabled=True,
            active=True,
            lingering=True,
            agent_name="athena",
            status="linger",
            text="HELLO FROM TURN\n— turn complete —",
            linger_until=now + 60.0,
        )
    w._prev_scroll["athena"] = "Processing message in LIVE mode\n"
    w.tick(
        selected_agent="athena",
        broca_scrollback=(
            "Processing message in LIVE mode\n"
            'HTTP Request: POST http://localhost:8284/v1/agents/a/messages "HTTP/1.1 200 OK"\n'
            "Processing message in LIVE mode\n"  # even a real-looking line
        ),
        now=now,
    )
    snap = w.snapshot()
    assert snap.status == "linger"
    assert "HELLO FROM TURN" in snap.text
    assert "waiting for Letta run" not in snap.text


def test_empty_hardcopy_does_not_reset_cursor(tmp_path: Path):
    """Empty hardcopy must not arm first-paint skip on the next full capture."""
    w = TurnStreamWorker(agents_root=tmp_path)
    prev = "idle\n"
    w.tick(selected_agent="athena", broca_scrollback=prev)
    w.tick(selected_agent="athena", broca_scrollback="")  # race
    w.tick(
        selected_agent="athena",
        broca_scrollback=prev + "Processing message in LIVE mode\n",
    )
    snap = w.snapshot()
    assert snap.active is True
    assert snap.status in ("seeking", "error")


def test_linger_expiry_clears(tmp_path: Path):
    w = TurnStreamWorker(agents_root=tmp_path)
    now = time.time()
    with w._lock:
        w.state = TurnStreamState(
            enabled=True,
            active=True,
            lingering=True,
            agent_name="athena",
            status="linger",
            text="done",
            linger_until=now - 1.0,
        )
    w.tick(selected_agent="athena", broca_scrollback="x", now=now)
    snap = w.snapshot()
    assert snap.active is False
    assert snap.status == "idle"
    assert snap.text == ""


def test_messages_to_text_formats_rows():
    from stop.turn_stream import messages_to_text

    text = messages_to_text(
        [
            {"message_type": "reasoning_message", "reasoning": "plan"},
            {"message_type": "assistant_message", "content": "hello there"},
            {"message_type": "stop_reason", "stop_reason": "end_turn"},
        ]
    )
    assert "[think] plan" in text
    assert "hello there" in text


def test_clean_and_extract_user_query():
    from stop.turn_stream import (
        clean_user_query,
        extract_user_query,
        waiting_panel_text,
        with_query_header,
    )

    raw = (
        "[Username: @AskDoctorBitcoin, Telegram ID: 1] "
        "hey can you check the meters?"
    )
    assert clean_user_query(raw) == "hey can you check the meters?"
    q = extract_user_query(
        [
            {
                "message_type": "user_message",
                "content": "[Username: @otto, Otto_Bridge ID: otto] ping please",
            },
            {"message_type": "assistant_message", "content": "pong"},
        ]
    )
    assert q == "ping please"
    wait = waiting_panel_text(agent_name="athena", run_id="run-abc", query=q)
    assert wait.startswith("> ping please")
    assert "waiting for first model step" in wait
    body = with_query_header("[think] hmm\nOK", q)
    assert body.startswith("> ping please\n\n[think]")
