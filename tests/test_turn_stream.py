"""Unit tests for Broca→Letta turn-stream worker (no network)."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from stop.turn_stream import (
    TurnStreamState,
    TurnStreamWorker,
    format_stream_event,
    load_agent_creds,
    log_line_is_fresh,
    scrollback_signals_turn_start,
)


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _live(epoch: float, msg: str = "Processing message in LIVE mode") -> str:
    return f"[{_ts(epoch)}] INFO {msg}\n"


def test_scrollback_detects_new_turn_line():
    now = time.time()
    prev = f"{_ts(now - 5)} INFO idle\n{_ts(now - 4)} INFO waiting\n"
    new = prev + _live(now)
    assert scrollback_signals_turn_start(prev, new, now=now) is True


def test_scrollback_rejects_stale_live_timestamp():
    """Reload thrash resurfaces old LIVE rows — must not arm the popup."""
    now = time.time()
    prev = "idle\n"
    stale = prev + _live(now - 600)  # 10 minutes ago
    assert scrollback_signals_turn_start(prev, stale, now=now) is False


def test_scrollback_rejects_untimestamped_live():
    now = time.time()
    prev = "idle\n"
    new = prev + "Processing message in LIVE mode\n"
    assert scrollback_signals_turn_start(prev, new, now=now) is False


def test_log_line_is_fresh_window():
    now = time.time()
    assert log_line_is_fresh(_live(now).rstrip(), now=now) is True
    assert log_line_is_fresh(_live(now - 59).rstrip(), now=now) is True
    assert log_line_is_fresh(_live(now - 90).rstrip(), now=now) is False
    assert log_line_is_fresh("Processing message in LIVE mode", now=now) is False


def test_scrollback_ignores_identical():
    text = "Processing message in LIVE mode\n"
    assert scrollback_signals_turn_start(text, text) is False


def test_scrollback_ignores_noise_delta():
    prev = "line1\n"
    new = prev + "HTTP Request: GET http://127.0.0.1:8871/health\n"
    assert scrollback_signals_turn_start(prev, new) is False


def test_scrollback_ignores_lagging_http_post_ok():
    """POST …/messages 200 is logged when the Letta call finishes — not a new turn."""
    now = time.time()
    prev = _live(now - 10)
    new = (
        prev
        + f'[{_ts(now)}] INFO HTTP Request: POST http://localhost:8284/v1/agents/agent-x/messages "HTTP/1.1 200 OK"\n'
    )
    assert scrollback_signals_turn_start(prev, new, now=now) is False


def test_scrollback_ignores_dequeue_alone():
    prev = "idle\n"
    new = prev + "Atomically dequeued message (Queue ID: abc)\n"
    assert scrollback_signals_turn_start(prev, new) is False


def test_scrollback_ignores_old_live_mode_on_unstable_hardcopy():
    """Hardcopy rewrite with old LIVE lines still in the tail must not re-trap."""
    now = time.time()
    old = now - 3600
    live = (
        f"[{_ts(old)}] INFO Processing message in LIVE mode\n"
        f"[{_ts(old)}] INFO Processing message with attached core block\n"
        f"[{_ts(old + 20)}] INFO Routing response through otto_bridge handler\n"
    )
    prev = "earlier\n" + live
    # Unstable: dropped 'earlier', same LIVE lines still present, plus noise.
    new = live + f"[{_ts(old + 60)}] INFO some keepalive\n"
    assert scrollback_signals_turn_start(prev, new, now=now) is False


def test_scrollback_detects_new_live_mode_amid_unstable_hardcopy():
    now = time.time()
    prev = (
        f"[{_ts(now - 120)}] INFO Processing message in LIVE mode\n"
        f"[{_ts(now - 100)}] INFO Routing response through otto_bridge handler\n"
    )
    new = (
        f"[{_ts(now - 100)}] INFO Routing response through otto_bridge handler\n"
        + _live(now)
    )
    assert scrollback_signals_turn_start(prev, new, now=now) is True


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
    now = time.time()
    text = _live(now)
    w.tick(selected_agent="athena", broca_scrollback=text, now=now)
    snap = w.snapshot()
    assert snap.active is False
    assert snap.status == "idle"


def test_worker_second_tick_with_turn_seeks_without_creds(tmp_path: Path):
    w = TurnStreamWorker(agents_root=tmp_path)
    now = time.time()
    prev = "idle\n"
    w.tick(selected_agent="athena", broca_scrollback=prev, now=now)
    w.tick(
        selected_agent="athena",
        broca_scrollback=prev + _live(now),
        now=now,
    )
    snap = w.snapshot()
    assert snap.active is True
    assert snap.status == "error"
    assert "no Letta creds" in snap.error


def test_toggle_off_clears_active(tmp_path: Path):
    w = TurnStreamWorker(agents_root=tmp_path)
    now = time.time()
    prev = "idle\n"
    w.tick(selected_agent="athena", broca_scrollback=prev, now=now)
    w.tick(
        selected_agent="athena",
        broca_scrollback=prev + _live(now),
        now=now,
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
    w._prev_scroll["athena"] = _live(now - 30)
    w.tick(
        selected_agent="athena",
        broca_scrollback=(
            _live(now - 30)
            + f'[{_ts(now)}] INFO HTTP Request: POST http://localhost:8284/v1/agents/a/messages "HTTP/1.1 200 OK"\n'
            + _live(now)  # even a real-looking fresh line
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
    now = time.time()
    prev = "idle\n"
    w.tick(selected_agent="athena", broca_scrollback=prev, now=now)
    w.tick(selected_agent="athena", broca_scrollback="", now=now)  # race
    w.tick(
        selected_agent="athena",
        broca_scrollback=prev + _live(now),
        now=now,
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


def test_fetch_broca_triggering_message_via_http(tmp_path: Path):
    """LIVE-mode trap reads the ask from Otto bridge HTTP, not sanctum.db."""
    import json
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    from stop.turn_stream import fetch_broca_triggering_message

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path != "/v1/turn/current":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(
                {
                    "active": True,
                    "message": "[Username: @x] TRIGGER_FROM_HTTP please",
                    "status": "processing",
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):  # noqa: ANN002
            return

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    broca = tmp_path / "athena" / "broca"
    broca.mkdir(parents=True)
    (broca / ".env").write_text(
        f"OTTO_BRIDGE_HTTP_LISTEN=127.0.0.1:{port}\n"
        "OTTO_BRIDGE_HTTP_API_KEY=testkey\n",
        encoding="utf-8",
    )
    try:
        q = fetch_broca_triggering_message(tmp_path, "athena")
        assert q == "TRIGGER_FROM_HTTP please"
    finally:
        srv.shutdown()


def test_background_agent_turn_does_not_replace_focused(tmp_path: Path):
    w = TurnStreamWorker(agents_root=tmp_path)
    now = time.time()
    w.tick(
        selected_agent="ada",
        broca_by_agent={"ada": "idle\n", "rico": "idle\n"},
        now=now,
    )
    # Focused ada is seeking/erroring (no creds).
    w.tick(
        selected_agent="ada",
        broca_by_agent={
            "ada": "idle\n" + _live(now),
            "rico": "idle\n",
        },
        now=now,
    )
    assert w.snapshot().status in ("seeking", "error")
    assert w.snapshot().agent_name == "ada"
    # While busy, rico turn becomes pending badge only.
    with w._lock:
        w.state.status = "streaming"
        w.state.active = True
        w.state.agent_name = "ada"
    w.tick(
        selected_agent="ada",
        broca_by_agent={
            "ada": "idle\n" + _live(now) + "more\n",
            "rico": "idle\n" + _live(now),
        },
        now=now,
    )
    snap = w.snapshot()
    assert snap.agent_name == "ada"
    assert "rico" in snap.pending_agents


def test_run_still_in_flight_and_status_helpers():
    from stop.turn_stream import bounded_turn_text, run_still_in_flight

    assert run_still_in_flight("running") is True
    assert run_still_in_flight("created") is True
    assert run_still_in_flight("") is True  # unknown → keep waiting
    assert run_still_in_flight("completed") is False
    assert run_still_in_flight("failed") is False
    bounded = bounded_turn_text("r" * 20000 + "\nFINAL", "the query", max_chars=200)
    assert bounded.startswith("> the query\n\n")
    assert bounded.endswith("FINAL")
    assert len(bounded) == 200


def test_consume_stream_eof_while_running_keeps_going_then_merges_final(
    tmp_path: Path, monkeypatch
):
    """SSE EOF mid-think must not finalize until the run completes; final
    assistant text comes from the messages API even if [think] was long."""
    from stop.turn_stream import LettaAgentCreds, TurnStreamWorker

    creds = LettaAgentCreds(
        agent_name="athena",
        agent_id="agent-x",
        api_key="k",
        endpoint="http://127.0.0.1:9",
    )
    w = TurnStreamWorker(agents_root=tmp_path)
    w.STREAM_WALL_S = 8.0
    w.SSE_READ_TIMEOUT_S = 1.0
    w.LINGER_S = 30.0

    statuses = iter(["running", "running", "completed"])
    monkeypatch.setattr(
        "stop.turn_stream.fetch_run_status",
        lambda *a, **k: next(statuses, "completed"),
    )
    think = "[think] " + ("x" * 80)
    final = "FINAL_ASSISTANT_REPLY_HERE"
    msg_calls = {"n": 0}

    def fake_messages(*a, **k):
        msg_calls["n"] += 1
        if msg_calls["n"] < 3:
            return [
                {
                    "id": "t1",
                    "message_type": "reasoning_message",
                    "reasoning": "x" * 80,
                }
            ]
        return [
            {
                "id": "t1",
                "message_type": "reasoning_message",
                "reasoning": "x" * 80,
            },
            {
                "id": "a1",
                "message_type": "assistant_message",
                "content": final,
            },
        ]

    monkeypatch.setattr("stop.turn_stream.fetch_run_messages", fake_messages)

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def readline(self):
            # Immediate EOF → old bug treated this as stream_done.
            return b""

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: FakeResp(),
    )

    with w._lock:
        w.state.enabled = True
        w.state.active = True
        w.state.agent_name = "athena"
        w.state.query = "hi"
        # Longer partial reasoning used to beat the completed API payload.
        w.state.text = "[think] " + ("partial-but-longer-" * 700)
        w.state.status = "streaming"
        w._generation = 1

    t0 = time.time()
    w._consume_stream(creds, "run-1", gen=1)
    elapsed = time.time() - t0
    assert elapsed < 7.0, f"consume hung too long: {elapsed:.1f}s"
    snap = w.snapshot()
    assert snap.status == "linger"
    assert snap.active is True
    assert final in snap.text
    assert snap.text.startswith("> hi\n\n")
    assert "— turn complete —" in snap.text


def test_show_turn_stays_visible_when_active_even_if_text_empty():
    """Regression: empty text used to hide the overlay mid-think."""
    import asyncio
    from pathlib import Path

    from stop.app import StopApp, WindowPane
    from stop.host import FixtureHost
    from stop.turn_stream import TurnStreamState

    fixture = Path(__file__).parent / "fixtures" / "basic"

    async def run() -> None:
        app = StopApp(FixtureHost(fixture))
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause(0.1)
            pane = app.query_one(WindowPane)
            pane.show_turn(
                TurnStreamState(
                    enabled=True,
                    active=True,
                    agent_name="athena",
                    status="streaming",
                    text="",
                    started_at=time.time(),
                )
            )
            await pilot.pause(0.05)
            assert pane.query_one("#turn-panel").display is True

    asyncio.run(run())
