"""Unit tests for Broca→Letta turn-stream worker (no network)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
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


def test_load_agent_creds_from_parent_when_broca_has_no_letta_keys(tmp_path: Path):
    """Q / Porter / Wren: Letta identity lives in agents/<name>/.env."""
    root = tmp_path / "q"
    (root / "broca").mkdir(parents=True)
    (root / ".env").write_text(
        "AGENT_ID=agent-q\n"
        "AGENT_API_KEY=q-key\n"
        "AGENT_ENDPOINT=http://127.0.0.1:8284\n",
        encoding="utf-8",
    )
    (root / "broca" / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=not-a-letta-key\nLOG_LEVEL=INFO\n",
        encoding="utf-8",
    )
    creds = load_agent_creds(tmp_path, "q")
    assert creds is not None
    assert creds.agent_id == "agent-q"
    assert creds.api_key == "q-key"
    assert creds.endpoint == "http://127.0.0.1:8284"


def test_worker_default_enabled_and_toggle(tmp_path: Path):
    w = TurnStreamWorker(agents_root=tmp_path)
    assert w.snapshot().enabled is True
    assert w.snapshot().follow_all is False
    assert w.toggle() is False
    assert w.snapshot().enabled is False
    assert w.snapshot().status == "off"
    assert w.toggle() is True
    assert w.snapshot().enabled is True
    assert w.snapshot().follow_all is False


def test_follow_toggle_turns_off_selected_and_preempts(tmp_path: Path, monkeypatch):
    """``f`` follows any agent; a newer start drops the current popup."""
    from stop.turn_stream import TurnStreamWorker

    root = tmp_path
    for name in ("athena", "rico"):
        d = root / name / "broca"
        d.mkdir(parents=True)
        (d / ".env").write_text(
            f"AGENT_ID=agent-{name}\nAGENT_API_KEY=k\n"
            "AGENT_ENDPOINT=http://127.0.0.1:9\n",
            encoding="utf-8",
        )

    w = TurnStreamWorker(agents_root=root)
    seeks: list[str] = []

    def fake_start(creds, **kwargs):
        seeks.append(creds.agent_name)
        with w._lock:
            w.state.active = True
            w.state.status = "streaming"
            w.state.agent_name = creds.agent_name
            w.state.enabled = True

    def fake_preempt(name, **kwargs):
        seeks.append(f"preempt:{kwargs.get('from_agent', '')}->{name}")
        with w._lock:
            w.state.active = True
            w.state.status = "streaming"
            w.state.agent_name = name
            w.state.enabled = True
            w.state.follow_all = True

    monkeypatch.setattr(w, "_start_seek", fake_start)
    monkeypatch.setattr(w, "_preempt_and_seek", fake_preempt)

    assert w.toggle_follow() is True
    snap = w.snapshot()
    assert snap.enabled is True
    assert snap.follow_all is True

    # t from follow → selected mode (follow off).
    assert w.toggle() is True
    assert w.snapshot().follow_all is False
    assert w.snapshot().enabled is True
    assert w.toggle_follow() is True
    assert w.snapshot().follow_all is True

    now = time.time()
    w.tick(
        selected_agent="ada",
        broca_by_agent={"athena": _live(now)},
        now=now,
    )
    assert seeks == ["athena"]

    seeks.clear()
    w.tick(
        selected_agent="ada",
        broca_by_agent={
            "athena": _live(now) + "\nmore\n",
            "rico": _live(now + 1),
        },
        now=now + 1,
    )
    assert seeks == ["preempt:athena->rico"]


def test_selected_mode_does_not_preempt_for_background_agent(
    tmp_path: Path, monkeypatch
):
    root = tmp_path
    for name in ("athena", "rico"):
        d = root / name / "broca"
        d.mkdir(parents=True)
        (d / ".env").write_text(
            f"AGENT_ID=agent-{name}\nAGENT_API_KEY=k\n"
            "AGENT_ENDPOINT=http://127.0.0.1:9\n",
            encoding="utf-8",
        )
    w = TurnStreamWorker(agents_root=root)
    w.set_mode("selected")
    seeks: list[str] = []
    monkeypatch.setattr(
        w,
        "_start_seek",
        lambda creds, **k: seeks.append(creds.agent_name),
    )
    preempted = []
    monkeypatch.setattr(
        w,
        "_preempt_and_seek",
        lambda *a, **k: preempted.append(a),
    )
    now = time.time()
    with w._lock:
        w.state.active = True
        w.state.status = "streaming"
        w.state.agent_name = "athena"
    w._prev_scroll["athena"] = _live(now - 30)
    w.tick(
        selected_agent="athena",
        broca_by_agent={
            "athena": _live(now - 30) + "\nstill going\n",
            "rico": _live(now),
        },
        now=now,
    )
    assert preempted == []
    assert "rico" in w.snapshot().pending_agents
    assert seeks == []


def test_worker_first_paint_fires_only_when_live_line_is_fresh(tmp_path: Path):
    w = TurnStreamWorker(agents_root=tmp_path)
    now = time.time()
    text = _live(now)
    w.tick(selected_agent="athena", broca_scrollback=text, now=now)
    snap = w.snapshot()
    assert snap.active is True
    assert snap.status == "error"
    assert "no Letta creds" in snap.error

    stale = TurnStreamWorker(agents_root=tmp_path)
    stale.tick(
        selected_agent="athena",
        broca_scrollback=_live(now - 600),
        now=now,
    )
    assert stale.snapshot().active is False


def test_bridge_probe_recovers_turn_already_in_progress(tmp_path: Path, monkeypatch):
    from stop.turn_stream import LettaAgentCreds

    w = TurnStreamWorker(agents_root=tmp_path)
    started: list[dict] = []
    monkeypatch.setattr(
        "stop.turn_stream.fetch_broca_current_turn",
        lambda *a, **k: {
            "active": True,
            "message": "QUERY ALREADY RUNNING",
            "status": "processing",
            "queue_id": 42,
        },
    )
    monkeypatch.setattr(
        "stop.turn_stream.load_agent_creds",
        lambda *a, **k: LettaAgentCreds(
            agent_name="athena",
            agent_id="agent-x",
            api_key="k",
            endpoint="http://127.0.0.1:9",
        ),
    )

    def fake_start(creds, **kwargs):
        started.append(kwargs)

    monkeypatch.setattr(w, "_start_seek", fake_start)
    w.tick(selected_agent="athena", broca_scrollback="old idle log\n")
    deadline = time.time() + 1
    while not started and time.time() < deadline:
        time.sleep(0.01)
    assert started
    assert started[0]["initial_query"] == "QUERY ALREADY RUNNING"
    assert started[0]["recovery_query"] == "QUERY ALREADY RUNNING"


def test_recovery_rejects_zombies_and_matches_current_query(tmp_path: Path, monkeypatch):
    from stop.turn_stream import LettaAgentCreds, pick_run_id

    now = time.time()
    creds = LettaAgentCreds(
        agent_name="athena",
        agent_id="agent-x",
        api_key="k",
        endpoint="http://127.0.0.1:9",
    )

    def iso(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat()

    rows = [
        {
            "id": "zombie-toast",
            "agent_id": "agent-x",
            "status": "running",
            "background": True,
            "created_at": iso(now - 180 * 86400),
        },
        {
            "id": "fresh-wrong-query",
            "agent_id": "agent-x",
            "status": "running",
            "background": True,
            "created_at": iso(now - 30),
        },
        {
            "id": "fresh-current-query",
            "agent_id": "agent-x",
            "status": "running",
            "background": True,
            "created_at": iso(now - 90),
        },
    ]
    monkeypatch.setattr("stop.turn_stream._http_json", lambda *a, **k: rows)
    monkeypatch.setattr("stop.turn_stream.list_runs_for_agent", lambda *a, **k: [])

    messages = {
        "zombie-toast": [
            {"message_type": "user_message", "content": "Build the Toast API tool"}
        ],
        "fresh-wrong-query": [
            {"message_type": "user_message", "content": "Some other current turn"}
        ],
        "fresh-current-query": [
            {
                "message_type": "user_message",
                "content": "Athena, discuss our actual topic",
            }
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
            recovery_query="Athena, discuss our actual topic",
            now_epoch=now,
        )
        == "fresh-current-query"
    )


def test_normal_seek_rejects_old_active_even_when_letta_calls_it_running(
    monkeypatch,
):
    from stop.turn_stream import LettaAgentCreds, pick_run_id

    now = time.time()
    creds = LettaAgentCreds("athena", "agent-x", "k", "http://127.0.0.1:9")
    rows = [
        {
            "id": "six-month-zombie",
            "agent_id": "agent-x",
            "status": "running",
            "background": True,
            "created_at": datetime.fromtimestamp(
                now - 180 * 86400, timezone.utc
            ).isoformat(),
        }
    ]
    monkeypatch.setattr("stop.turn_stream._http_json", lambda *a, **k: rows)
    monkeypatch.setattr("stop.turn_stream.list_runs_for_agent", lambda *a, **k: [])
    assert (
        pick_run_id(
            creds,
            since_epoch=now,
            seen_run_ids=set(),
            now_epoch=now,
        )
        is None
    )


def test_finished_run_from_this_turn_is_found_when_query_matches(monkeypatch):
    """A turn that already completed is invisible to the 8s window.

    Athena's messages POST blocked ~50–100s. By the time hardcopy armed
    the popup, the run was finished and older than 8 seconds, so seek
    sat on the Broca query and timed out.
    """
    from stop.turn_stream import LettaAgentCreds, pick_run_id

    now = time.time()
    creds = LettaAgentCreds("athena", "agent-x", "k", "http://127.0.0.1:9")

    def iso(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat()

    rows = [
        {
            "id": "this-turn",
            "agent_id": "agent-x",
            "status": "completed",
            "background": True,
            "created_at": iso(now - 90),
        },
        {
            "id": "previous-turn",
            "agent_id": "agent-x",
            "status": "completed",
            "background": True,
            "created_at": iso(now - 400),
        },
    ]
    monkeypatch.setattr("stop.turn_stream._http_json", lambda *a, **k: [])
    monkeypatch.setattr("stop.turn_stream.list_runs_for_agent", lambda *a, **k: rows)
    messages = {
        "this-turn": [
            {"message_type": "user_message", "content": "How's your day been?"}
        ],
        "previous-turn": [
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
        == "this-turn"
    )
    assert (
        pick_run_id(
            creds,
            since_epoch=now,
            seen_run_ids=set(),
            now_epoch=now,
        )
        is None
    )


def test_fresh_running_run_kept_before_user_message_is_stored(monkeypatch):
    from stop.turn_stream import LettaAgentCreds, pick_run_id

    now = time.time()
    creds = LettaAgentCreds("athena", "agent-x", "k", "http://127.0.0.1:9")
    rows = [
        {
            "id": "just-started",
            "agent_id": "agent-x",
            "status": "running",
            "background": True,
            "created_at": datetime.fromtimestamp(now - 2, timezone.utc).isoformat(),
        }
    ]
    monkeypatch.setattr("stop.turn_stream._http_json", lambda *a, **k: rows)
    monkeypatch.setattr("stop.turn_stream.list_runs_for_agent", lambda *a, **k: [])
    monkeypatch.setattr("stop.turn_stream.fetch_run_messages", lambda *_a, **_k: [])
    assert (
        pick_run_id(
            creds,
            since_epoch=now,
            seen_run_ids=set(),
            recovery_query="How's your day been?",
            now_epoch=now,
        )
        == "just-started"
    )


def test_seek_deadline_follows_an_open_broca_turn():
    from stop.turn_stream import extend_seek_deadline

    since = 1_000.0
    # Broca still processing — do not die at the 45s mark.
    extended = extend_seek_deadline(
        since=since,
        now=since + 50,
        deadline=since + 45,
        broca_active=True,
        max_s=300,
    )
    assert extended == since + 53
    # Cap so a stuck processing flag cannot hold the popup forever.
    capped = extend_seek_deadline(
        since=since,
        now=since + 298,
        deadline=since + 45,
        broca_active=True,
        max_s=300,
    )
    assert capped == since + 300
    # Idle Broca but we already have the query from the log edge.
    assert (
        extend_seek_deadline(
            since=since,
            now=since + 50,
            deadline=since + 45,
            broca_active=False,
            max_s=300,
            have_query=True,
        )
        == since + 53
    )
    # Idle Broca and no query: leave the short deadline alone.
    assert (
        extend_seek_deadline(
            since=since,
            now=since + 50,
            deadline=since + 45,
            broca_active=False,
            max_s=300,
        )
        == since + 45
    )


def test_log_edge_seek_passes_broca_query(tmp_path: Path, monkeypatch):
    from stop.turn_stream import LettaAgentCreds, TurnStreamWorker

    w = TurnStreamWorker(agents_root=tmp_path)
    seen: list[str] = []

    def fake_seek(_creds, _since, _gen, recovery_query=""):
        seen.append(recovery_query)

    monkeypatch.setattr(w, "_seek_and_stream", fake_seek)
    monkeypatch.setattr(
        "stop.turn_stream.fetch_broca_triggering_message",
        lambda *_a, **_k: "How's your day been?",
    )
    creds = LettaAgentCreds("athena", "agent-x", "k", "http://127.0.0.1:9")
    w._start_seek(creds, since=time.time())
    deadline = time.time() + 1
    while not seen and time.time() < deadline:
        time.sleep(0.01)
    assert seen == ["How's your day been?"]


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


def test_wrapped_live_mode_line_arms_popup():
    """Broca's 80-column screen splits LIVE mode across two physical lines."""
    now = time.time()
    ts = _ts(now)
    prev = f"[{ts}] INFO idle\n"
    new = (
        prev
        + f"[{ts}] INFO runtime.core.queue: Processing message in LIVE m\n"
        + "ode (queue timeout 120s)\n"
    )
    assert scrollback_signals_turn_start(prev, new, now=now) is True


def test_linger_keeps_finished_text_until_a_new_turn(tmp_path: Path):
    """Repeated old lines during linger must not wipe the completed turn."""
    w = TurnStreamWorker(agents_root=tmp_path)
    now = time.time()
    old = _live(now - 30)
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
    w._prev_scroll["athena"] = old
    w.tick(
        selected_agent="athena",
        broca_scrollback=old
        + f'[{_ts(now)}] INFO HTTP Request: POST http://localhost:8284/v1/agents/a/messages "HTTP/1.1 200 OK"\n',
        now=now,
    )
    snap = w.snapshot()
    assert snap.status == "linger"
    assert "HELLO FROM TURN" in snap.text


def test_linger_yields_to_the_next_fresh_turn(tmp_path: Path):
    w = TurnStreamWorker(agents_root=tmp_path)
    now = time.time()
    old = _live(now - 30)
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
    w._prev_scroll["athena"] = old
    w.tick(
        selected_agent="athena",
        broca_scrollback=old + _live(now),
        now=now,
    )
    snap = w.snapshot()
    assert snap.status in ("seeking", "error")
    assert "HELLO FROM TURN" not in snap.text


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
    assert "Letta is thinking" in wait or "waiting for first model step" in wait
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

        def close(self):
            return None

        def readline(self):
            # Immediate EOF → old bug treated this as stream_done.
            return b""

    monkeypatch.setattr(
        "stop.turn_stream._safe_urlopen",
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


def test_consume_stream_idle_timeout_exits_when_run_already_done(
    tmp_path: Path, monkeypatch
):
    """SSE read timeout after the run finished must not soft-loop forever."""
    from stop.turn_stream import LettaAgentCreds, TurnStreamWorker

    creds = LettaAgentCreds("athena", "agent-x", "k", "http://127.0.0.1:9")
    w = TurnStreamWorker(agents_root=tmp_path)
    w.STREAM_WALL_S = 30.0
    w.SSE_READ_TIMEOUT_S = 1.0
    w.LINGER_S = 30.0
    monkeypatch.setattr(
        "stop.turn_stream.fetch_run_status", lambda *a, **k: "completed"
    )
    monkeypatch.setattr(
        "stop.turn_stream.fetch_run_messages",
        lambda *a, **k: [
            {"id": "a1", "message_type": "assistant_message", "content": "DONE"}
        ],
    )

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def close(self):
            return None

        def readline(self):
            raise TimeoutError("timed out")

    opens = {"n": 0}

    def fake_open(*a, **k):
        opens["n"] += 1
        if opens["n"] > 2:
            raise AssertionError("soft-loop reconnect after completed run")
        return FakeResp()

    monkeypatch.setattr("stop.turn_stream._safe_urlopen", fake_open)
    with w._lock:
        w.state.enabled = True
        w.state.active = True
        w.state.agent_name = "athena"
        w.state.query = "hi"
        w.state.text = "Turn started — waiting for Letta run (athena)…"
        w.state.status = "streaming"
        w._generation = 1
    t0 = time.time()
    w._consume_stream(creds, "run-1", gen=1)
    assert time.time() - t0 < 5.0
    assert opens["n"] == 1
    snap = w.snapshot()
    assert snap.status == "linger"
    assert "DONE" in snap.text


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
