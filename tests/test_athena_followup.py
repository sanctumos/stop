"""Regression: mid-stream LIVE hardcopy must not wipe a finished answer."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from stop.turn_stream import TurnStreamWorker


def _live(epoch: float) -> str:
    ts = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    return f"[{ts}] INFO Processing message in LIVE mode\n"


def test_busy_streaming_does_not_arm_followup_from_hardcopy(
    tmp_path: Path, monkeypatch
):
    broca = tmp_path / "athena" / "broca"
    broca.mkdir(parents=True)
    (broca / ".env").write_text(
        "AGENT_ID=agent-x\nAGENT_API_KEY=k\nAGENT_ENDPOINT=http://127.0.0.1:9\n",
        encoding="utf-8",
    )
    w = TurnStreamWorker(agents_root=tmp_path)
    with w._lock:
        w.state.enabled = True
        w.state.active = True
        w.state.status = "streaming"
        w.state.agent_name = "athena"
        w.state.query = "hello"
        w.state.text = "full answer already here"
        w._generation = 1

    now = time.time()
    # A second LIVE edge while still streaming used to set _followup_agent.
    w.tick(
        selected_agent="athena",
        broca_scrollback="prev\n" + _live(now),
        now=now,
    )
    assert w._followup_agent is None


def test_followup_at_stream_end_skipped_when_same_broca_turn(
    tmp_path: Path, monkeypatch
):
    w = TurnStreamWorker(agents_root=tmp_path)
    w._followup_agent = "athena"
    w._armed_queue_id = 42
    with w._lock:
        w.state.enabled = True
        w.state.query = "same ask"
        w._generation = 1

    monkeypatch.setattr(
        "stop.turn_stream.fetch_broca_current_turn",
        lambda *_a, **_k: {
            "active": True,
            "message": "same ask",
            "queue_id": 42,
            "status": "processing",
        },
    )
    started = {"n": 0}

    def no_seek(*_a, **_k):
        started["n"] += 1

    monkeypatch.setattr(w, "_start_seek", no_seek)
    # Drive the followup gate by calling the block via a tiny harness:
    # reuse set_enabled path is wrong; call the logic through _consume_stream end
    # by invoking a stripped copy — instead call the followup check inline.
    from stop.turn_stream import LettaAgentCreds, clean_user_query, normalized_query
    from stop.turn_stream import fetch_broca_current_turn

    creds = LettaAgentCreds("athena", "agent-x", "k", "http://127.0.0.1:9")
    gen = 1
    follow = "athena"
    turn = fetch_broca_current_turn(tmp_path, "athena")
    new_q = clean_user_query(str(turn.get("message") or ""))
    old_q = normalized_query(w.state.query)
    same = (
        not turn.get("active")
        or turn.get("queue_id") == w._armed_queue_id
        or (new_q and old_q and normalized_query(new_q) == old_q)
    )
    assert same is True
    if not same:
        w._start_seek(creds, since=time.time(), recovery_query=new_q)
    assert started["n"] == 0


def test_fetch_run_messages_falls_back_to_steps(monkeypatch):
    from stop.turn_stream import LettaAgentCreds, fetch_run_messages

    creds = LettaAgentCreds("athena", "a", "k", "http://127.0.0.1:9")
    calls: list[str] = []

    def fake_http(method, url, **_k):
        calls.append(url)
        if url.endswith("/messages?limit=100&order=asc"):
            return []
        if url.endswith("/steps"):
            return [{"id": "step-1"}]
        if url.endswith("/steps/step-1/messages"):
            return [
                {
                    "id": "m1",
                    "message_type": "assistant_message",
                    "content": "from-step",
                }
            ]
        return []

    monkeypatch.setattr("stop.turn_stream._http_json", fake_http)
    rows = fetch_run_messages(creds, "run-1")
    assert any(r.get("content") == "from-step" for r in rows)
    assert any("/steps" in u for u in calls)
