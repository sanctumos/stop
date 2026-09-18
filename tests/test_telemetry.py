"""The trace ring forgets. It does not accumulate."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from stop.telemetry import TraceRing


def test_old_events_are_dropped(tmp_path):
    ring = TraceRing(tmp_path / "trace.json", max_age_s=10, load=False)
    ring.event("turn", "old")
    with ring._lock:
        ring._events[0]["ts"] = time.time() - 30
    ring.event("turn", "new", k=1)
    assert [row["event"] for row in ring.snapshot()] == ["new"]


def test_count_cap_drops_oldest(tmp_path):
    ring = TraceRing(
        tmp_path / "trace.json", max_events=3, coalesce_s=0, load=False
    )
    for i in range(10):
        ring.event("turn", "e", i=i)
    rows = ring.snapshot()
    assert len(rows) == 3
    assert [row["detail"]["i"] for row in rows] == [7, 8, 9]


def test_byte_cap_and_file_does_not_append_forever(tmp_path):
    path = tmp_path / "trace.json"
    ring = TraceRing(
        path,
        max_events=50,
        max_bytes=900,
        coalesce_s=0,
        load=False,
    )
    for i in range(30):
        ring._last_flush = 0
        ring.event("turn", "blob", i=i, pad="x" * 80)
    raw = path.read_bytes()
    assert len(raw) <= 900
    data = json.loads(raw)
    assert data["v"] == 1
    assert len(data["events"]) == len(ring.snapshot())
    assert len(data["events"]) < 30
    size_after_first_burst = path.stat().st_size
    for i in range(30, 60):
        ring._last_flush = 0
        ring.event("turn", "blob", i=i, pad="y" * 80)
    assert path.stat().st_size <= max(900, size_after_first_burst + 50)
    assert path.read_text(encoding="utf-8").count('"event"') == len(ring.snapshot())


def test_secrets_are_stripped_and_strings_capped(tmp_path):
    ring = TraceRing(tmp_path / "trace.json", load=False)
    ring.event(
        "turn",
        "e",
        api_key="do-not-keep",
        note="ok",
        blob="z" * 500,
    )
    detail = ring.snapshot()[0]["detail"]
    assert "api_key" not in detail
    assert detail["note"] == "ok"
    assert len(detail["blob"]) <= 240
    dumped = (tmp_path / "trace.json").read_text(encoding="utf-8")
    assert "do-not-keep" not in dumped


def test_identical_decisions_collapse(tmp_path):
    ring = TraceRing(tmp_path / "trace.json", coalesce_s=30, load=False)
    ring.event("turn", "pick", chosen="", http="")
    ring.event("turn", "pick", chosen="", http="")
    ring.event("turn", "pick", chosen="", http="")
    rows = ring.snapshot()
    assert len(rows) == 1
    assert rows[0]["n"] == 3


def test_disabled_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STOP_TRACE", "0")
    ring = TraceRing(tmp_path / "trace.json", load=False)
    ring.event("turn", "e", i=1)
    assert ring.snapshot() == []


def test_reload_forgets_expired_rows(tmp_path):
    path = tmp_path / "trace.json"
    path.write_text(
        json.dumps(
            {
                "v": 1,
                "events": [
                    {
                        "ts": time.time() - 10_000,
                        "component": "turn",
                        "event": "stale",
                        "n": 1,
                        "detail": {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ring = TraceRing(path, max_age_s=60, load=True)
    assert ring.snapshot() == []


def test_pick_records_why_a_finished_run_was_chosen(monkeypatch):
    from stop.turn_stream import LettaAgentCreds, pick_run_id

    now = time.time()
    creds = LettaAgentCreds("athena", "agent-x", "k", "http://127.0.0.1:9")
    seen: list[dict] = []

    def capture(component, event, **detail):
        seen.append({"component": component, "event": event, **detail})

    monkeypatch.setattr("stop.turn_stream.trace", capture)

    def iso(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat()

    rows = [
        {
            "id": "run-this-turn-aaaa",
            "agent_id": "agent-x",
            "status": "completed",
            "background": True,
            "created_at": iso(now - 90),
        },
        {
            "id": "run-previous-bbbb",
            "agent_id": "agent-x",
            "status": "completed",
            "background": True,
            "created_at": iso(now - 400),
        },
    ]
    monkeypatch.setattr("stop.turn_stream._http_json", lambda *a, **k: [])
    monkeypatch.setattr("stop.turn_stream.list_runs_for_agent", lambda *a, **k: rows)
    messages = {
        "run-this-turn-aaaa": [
            {"message_type": "user_message", "content": "How's your day been?"}
        ],
        "run-previous-bbbb": [
            {"message_type": "user_message", "content": "Earlier question"}
        ],
    }
    monkeypatch.setattr(
        "stop.turn_stream.fetch_run_messages",
        lambda _creds, rid: messages[rid],
    )
    assert (
        pick_run_id(
            creds,
            since_epoch=now,
            seen_run_ids=set(),
            recovery_query="How's your day been?",
            now_epoch=now,
        )
        == "run-this-turn-aaaa"
    )
    assert seen
    assert seen[-1]["event"] == "pick"
    assert seen[-1]["chosen"] == "run-this-turn-aaaa"[-12:]
    whys = {row["why"] for row in seen[-1]["near"]}
    assert "query" in whys
