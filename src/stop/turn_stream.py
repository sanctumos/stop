"""Broca turn-start trap → Letta run SSE stream for the selected agent."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Broca console lines that mean a Letta turn just began.
# Keep this tight: httpx "POST …/messages 200" logs when the request *finishes*
# (after the turn), and would re-trigger a seek that wipes the linger panel.
_TURN_START_RES = [
    re.compile(p, re.I)
    for p in (
        r"Processing message in LIVE mode",
        r"Processing message with attached core block",
    )
]

_TURN_END_BROCA_RES = [
    re.compile(p, re.I)
    for p in (
        r"Routing response through \w+ handler",
        r"Detaching core block",
    )
]


@dataclass
class LettaAgentCreds:
    agent_name: str
    agent_id: str
    api_key: str
    endpoint: str  # e.g. http://127.0.0.1:8284


@dataclass
class TurnStreamState:
    """Snapshot for the UI thread."""

    enabled: bool = True
    active: bool = False
    lingering: bool = False
    agent_name: str = ""
    run_id: str = ""
    status: str = "idle"  # idle | seeking | streaming | linger | error
    text: str = ""
    query: str = ""  # triggering user message, shown while waiting / as header
    saw_waiting_query: bool = False  # True once waiting pane showed `> query`
    error: str = ""
    started_at: float = 0.0
    linger_until: float = 0.0


def load_agent_creds(agents_root: Path, agent_name: str) -> LettaAgentCreds | None:
    """Read AGENT_ID / AGENT_API_KEY / AGENT_ENDPOINT from agents/<name>/broca/.env."""
    env_path = agents_root / agent_name / "broca" / ".env"
    if not env_path.is_file():
        # Some agents keep .env one level up.
        env_path = agents_root / agent_name / ".env"
    if not env_path.is_file():
        return None
    vals: dict[str, str] = {}
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            vals[k.strip()] = v.strip().strip("'").strip('"')
    except OSError:
        return None
    agent_id = vals.get("AGENT_ID") or ""
    api_key = vals.get("AGENT_API_KEY") or vals.get("LETTA_SERVER_PASSWORD") or ""
    endpoint = (vals.get("AGENT_ENDPOINT") or "http://127.0.0.1:8284").rstrip("/")
    # AGENT_ENDPOINT sometimes includes /v1 — normalize to server root.
    if endpoint.endswith("/v1"):
        endpoint = endpoint[:-3]
    if not agent_id or not api_key:
        return None
    return LettaAgentCreds(
        agent_name=agent_name,
        agent_id=agent_id,
        api_key=api_key,
        endpoint=endpoint,
    )


def scrollback_signals_turn_start(prev: str, new: str) -> bool:
    """True when new Broca scrollback added a turn-start line."""
    if not (new or "").strip():
        return False
    # hardcopy can embed NULs / C1 controls — normalize before compare.
    prev = (prev or "").replace("\x00", "")
    new = (new or "").replace("\x00", "")
    old_lines = prev.splitlines()
    new_lines = new.splitlines()
    if new_lines == old_lines:
        return False
    if len(new_lines) >= len(old_lines) and new_lines[: len(old_lines)] == old_lines:
        delta = new_lines[len(old_lines) :]
    else:
        delta = new_lines[-12:]
    return any(
        any(r.search(ln) for r in _TURN_START_RES) for ln in delta if ln.strip()
    )


def format_stream_event(obj: dict) -> str:
    """Reduce a Letta SSE JSON object to display text."""
    mtype = obj.get("message_type") or obj.get("type") or ""
    if mtype in ("ping", "keepalive", "user_message", "system_message"):
        return ""
    if mtype == "reasoning_message":
        reasoning = (obj.get("reasoning") or "").strip()
        if not reasoning:
            return ""
        return f"[think] {reasoning}"
    if mtype == "assistant_message":
        content = obj.get("content")
        if isinstance(content, list):
            bits = []
            for part in content:
                if isinstance(part, dict) and part.get("text"):
                    bits.append(str(part["text"]))
                elif isinstance(part, str):
                    bits.append(part)
            content = "".join(bits)
        text = (content or obj.get("message") or "").strip()
        return text
    if mtype == "tool_call_message":
        tc = obj.get("tool_call") or {}
        name = tc.get("name") or "?"
        return f"[tool → {name}]"
    if mtype == "tool_return_message":
        return "[tool ←]"
    if mtype in ("stop_reason", "usage_statistics"):
        return ""
    # Token-level deltas sometimes use different shapes.
    if "content" in obj and isinstance(obj["content"], str) and obj["content"]:
        return obj["content"]
    return ""


_USER_META_PREFIX_RE = re.compile(
    r"^\[(?:Username|Telegram|Otto_Bridge|Platform|sender)[^\]]*\]\s*",
    re.I,
)


def clean_user_query(content: str) -> str:
    """Strip Broca/Letta envelope prefixes from a user_message body."""
    text = (content or "").strip()
    if not text:
        return ""
    # Unwrap list-shaped content defensively.
    while True:
        m = _USER_META_PREFIX_RE.match(text)
        if not m:
            break
        text = text[m.end() :].lstrip()
    return text.strip()


def broca_db_path(agents_root: Path, agent_name: str) -> Path | None:
    """Path to agents/<name>/broca/sanctum.db when present."""
    p = agents_root / agent_name / "broca" / "sanctum.db"
    return p if p.is_file() else None


def fetch_broca_triggering_message(
    agents_root: Path, agent_name: str
) -> str:
    """Read the user message that triggered the current Broca turn.

    Console hardcopy only shows LIVE-mode lines — not the ask text. The ask
    lives in Broca's ``messages`` table (joined via ``queue`` while processing).
    Read-only. Prefer in-flight queue rows, else the latest user message.
    """
    db = broca_db_path(agents_root, agent_name)
    if db is None:
        return ""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return ""
    try:
        # In-flight turn: queue row → messages.message
        row = con.execute(
            """
            SELECT m.message
            FROM queue q
            JOIN messages m ON m.id = q.message_id
            WHERE q.status IN ('processing', 'pending', 'queued')
              AND m.role = 'user'
              AND IFNULL(m.message, '') != ''
            ORDER BY q.id DESC
            LIMIT 1
            """
        ).fetchone()
        if row and (row[0] or "").strip():
            return clean_user_query(str(row[0]))
        # Just-completed race: Broca may mark completed before our trap paints.
        # Take the newest user message from the last few seconds of queue work.
        row = con.execute(
            """
            SELECT m.message
            FROM queue q
            JOIN messages m ON m.id = q.message_id
            WHERE m.role = 'user'
              AND IFNULL(m.message, '') != ''
            ORDER BY q.id DESC
            LIMIT 1
            """
        ).fetchone()
        if row and (row[0] or "").strip():
            return clean_user_query(str(row[0]))
        row = con.execute(
            """
            SELECT message FROM messages
            WHERE role = 'user' AND IFNULL(message, '') != ''
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if row and (row[0] or "").strip():
            return clean_user_query(str(row[0]))
    except sqlite3.Error:
        return ""
    finally:
        con.close()
    return ""


def extract_user_query(rows: list[dict]) -> str:
    """First user_message content from a run messages list."""
    for obj in rows:
        if not isinstance(obj, dict):
            continue
        mtype = obj.get("message_type") or obj.get("type") or ""
        if mtype != "user_message":
            continue
        content = obj.get("content")
        if isinstance(content, list):
            bits = []
            for part in content:
                if isinstance(part, dict) and part.get("text"):
                    bits.append(str(part["text"]))
                elif isinstance(part, str):
                    bits.append(part)
            content = "".join(bits)
        text = clean_user_query(str(content or obj.get("message") or ""))
        if text:
            return text
    return ""


def waiting_panel_text(*, agent_name: str, run_id: str = "", query: str = "") -> str:
    """Body shown before the first assistant/reasoning step arrives."""
    q = (query or "").strip()
    if run_id:
        wait = (
            f"Live on {run_id}…\n"
            "(step stream — waiting for first model step)"
        )
    else:
        wait = f"Turn started — waiting for Letta run ({agent_name})…"
    if q:
        # Keep query readable; cap very long pastes.
        shown = q if len(q) <= 2000 else q[:2000] + "…"
        return f"> {shown}\n\n{wait}"
    return wait


def with_query_header(body: str, query: str) -> str:
    """Prefix stream/linger body with the triggering user query."""
    q = (query or "").strip()
    body = (body or "").rstrip()
    if not q:
        return body
    shown = q if len(q) <= 2000 else q[:2000] + "…"
    header = f"> {shown}"
    if body.startswith(header):
        return body
    if not body:
        return header
    return f"{header}\n\n{body}"


def _http_json(
    method: str,
    url: str,
    *,
    api_key: str,
    body: dict | None = None,
    timeout: float = 10.0,
) -> object:
    data = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    if not raw.strip():
        return None
    return json.loads(raw)


def list_runs_for_agent(
    creds: LettaAgentCreds, *, limit: int = 15
) -> list[dict]:
    """Fetch recent runs for this agent (server-side agent_id filter)."""
    q = urllib.parse.urlencode(
        {"limit": str(limit), "agent_id": creds.agent_id, "order": "desc"}
    )
    url = f"{creds.endpoint}/v1/runs/?{q}"
    try:
        data = _http_json("GET", url, api_key=creds.api_key, timeout=8.0)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        # Fallback without agent_id if older Letta rejects the param.
        try:
            q2 = urllib.parse.urlencode({"limit": str(max(limit, 50))})
            data = _http_json(
                "GET",
                f"{creds.endpoint}/v1/runs/?{q2}",
                api_key=creds.api_key,
                timeout=8.0,
            )
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            json.JSONDecodeError,
        ):
            return []
    rows: list
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("runs") or data.get("data") or []
    else:
        rows = []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get("agent_id") == creds.agent_id:
            out.append(r)
    return out


def fetch_run_messages(creds: LettaAgentCreds, run_id: str) -> list[dict]:
    """GET /v1/runs/{id}/messages — fallback when SSE yields nothing."""
    url = f"{creds.endpoint}/v1/runs/{run_id}/messages?limit=100&order=asc"
    try:
        data = _http_json("GET", url, api_key=creds.api_key, timeout=12.0)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        rows = data.get("messages") or data.get("data") or []
        return [x for x in rows if isinstance(x, dict)]
    return []


def messages_to_text(rows: list[dict]) -> str:
    parts: list[str] = []
    for obj in rows:
        piece = format_stream_event(obj)
        if piece:
            parts.append(piece)
    return "\n\n".join(parts).strip()


def pick_run_id(
    creds: LettaAgentCreds,
    *,
    since_epoch: float,
    seen_run_ids: set[str],
) -> str | None:
    """Choose a fresh background run for this agent after turn start."""
    # Active first (scoped when API allows).
    active: object = []
    try:
        q = urllib.parse.urlencode({"agent_id": creds.agent_id})
        active = _http_json(
            "GET",
            f"{creds.endpoint}/v1/runs/active?{q}",
            api_key=creds.api_key,
            timeout=5.0,
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        try:
            active = _http_json(
                "GET",
                f"{creds.endpoint}/v1/runs/active",
                api_key=creds.api_key,
                timeout=5.0,
            )
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            json.JSONDecodeError,
        ):
            active = []
    candidates: list[dict] = []
    if isinstance(active, list):
        candidates.extend(active)
    candidates.extend(list_runs_for_agent(creds, limit=25))

    best: tuple[float, str] | None = None
    # Hardcopy lag + long turns: Broca trap can fire tens of seconds after
    # created_at. Keep a wide floor so we still catch the run.
    floor = since_epoch - 180.0
    for r in candidates:
        if not isinstance(r, dict):
            continue
        if r.get("agent_id") != creds.agent_id:
            continue
        rid = r.get("id") or ""
        if not rid or rid in seen_run_ids:
            continue
        # Prefer background runs (required for /stream observer).
        if r.get("background") is False:
            continue
        created = r.get("created_at") or ""
        created_epoch = _parse_iso(created)
        status = (r.get("status") or "").lower()
        # Prefer in-flight runs. Completed runs are catch-up only for *this*
        # turn — a prior finished run inside the wide floor must not steal the
        # pane (that showed the previous query while waiting).
        if status in ("running", "created"):
            if created_epoch and created_epoch < floor:
                continue
            rank = 2
        elif status in ("completed", "succeeded"):
            if not created_epoch or created_epoch < since_epoch - 8.0:
                continue
            rank = 1
        else:
            continue
        score = rank * 1e12 + (created_epoch or 0.0)
        if best is None or score > best[0]:
            best = (score, rid)
    return best[1] if best else None


def _parse_iso(s: str) -> float:
    if not s:
        return 0.0
    try:
        # 2026-09-17T23:57:31.231032Z
        from datetime import datetime

        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


class TurnStreamWorker:
    """Background SSE reader; UI polls `.state`."""

    LINGER_S = 60.0
    SEEK_TIMEOUT_S = 45.0

    def __init__(
        self,
        *,
        agents_root: Path,
        on_update: Callable[[], None] | None = None,
    ):
        self.agents_root = agents_root
        self.on_update = on_update
        self._lock = threading.Lock()
        self.state = TurnStreamState()
        self._prev_scroll: dict[str, str] = {}
        self._seen_runs: set[str] = set()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._seek_agent: str | None = None
        self._seek_since: float = 0.0

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.state.enabled = enabled
            if not enabled:
                self._stop_event.set()
                self.state.active = False
                self.state.lingering = False
                self.state.status = "off"
                self.state.text = ""
                self.state.query = ""
                self.state.saw_waiting_query = False
                self.state.error = ""
                self.state.run_id = ""
                self._seek_agent = None

    def toggle(self) -> bool:
        with self._lock:
            new = not self.state.enabled
        self.set_enabled(new)
        return new

    def snapshot(self) -> TurnStreamState:
        with self._lock:
            s = self.state
            return TurnStreamState(
                enabled=s.enabled,
                active=s.active,
                lingering=s.lingering,
                agent_name=s.agent_name,
                run_id=s.run_id,
                status=s.status,
                text=s.text,
                query=s.query,
                saw_waiting_query=s.saw_waiting_query,
                error=s.error,
                started_at=s.started_at,
                linger_until=s.linger_until,
            )

    def tick(
        self,
        *,
        selected_agent: str | None,
        broca_scrollback: str,
        now: float | None = None,
    ) -> None:
        """Called each host refresh from the UI thread."""
        now = now or time.time()
        with self._lock:
            enabled = self.state.enabled
            lingering = self.state.lingering
            linger_until = self.state.linger_until
            status = self.state.status

        if not enabled:
            return

        if lingering and now >= linger_until:
            with self._lock:
                self.state.lingering = False
                self.state.active = False
                self.state.status = "idle"
                self.state.text = ""
                self.state.query = ""
                self.state.saw_waiting_query = False
                self.state.run_id = ""
                self.state.agent_name = ""
            # Advance scroll cursor so lagging Broca lines (POST 200, detach)
            # that arrived during linger do not look like a fresh turn start.
            if selected_agent and (broca_scrollback or "").strip():
                self._prev_scroll[selected_agent] = broca_scrollback
            self._notify()
            return

        # Busy on a turn (including linger/error hold) — never re-trap.
        if (
            lingering
            or status in ("streaming", "seeking", "linger", "error")
            or (self._thread and self._thread.is_alive())
        ):
            # Only advance cursor on real scrollback — empty hardcopy races
            # must not wipe prev (that makes the next full capture look like
            # first-paint and we miss the next turn forever).
            if selected_agent and (broca_scrollback or "").strip():
                self._prev_scroll[selected_agent] = broca_scrollback
            return

        if not selected_agent:
            return

        new = broca_scrollback or ""
        if not new.strip():
            # Transient empty hardcopy — keep cursor, do not arm first-paint.
            return

        prev = self._prev_scroll.get(selected_agent)
        if prev is None:
            # First real paint — don't treat full history as a new turn.
            self._prev_scroll[selected_agent] = new
            return

        self._prev_scroll[selected_agent] = new
        if not scrollback_signals_turn_start(prev, new):
            return

        creds = load_agent_creds(self.agents_root, selected_agent)
        if creds is None:
            with self._lock:
                self.state.status = "error"
                self.state.error = f"no Letta creds for {selected_agent}"
                self.state.active = True
                self.state.agent_name = selected_agent
                self.state.linger_until = now + 8.0
                self.state.lingering = True
            self._notify()
            return

        self._start_seek(creds, since=now)

    def _start_seek(self, creds: LettaAgentCreds, *, since: float) -> None:
        self._stop_event.clear()
        # Console trap has no ask text — pull it from Broca sanctum.db immediately
        # so the waiting pane shows `> query` before the Letta run exists.
        broca_q = fetch_broca_triggering_message(self.agents_root, creds.agent_name)
        with self._lock:
            self.state.active = True
            self.state.lingering = False
            self.state.agent_name = creds.agent_name
            self.state.run_id = ""
            self.state.query = broca_q
            self.state.saw_waiting_query = bool(broca_q)
            self.state.status = "seeking"
            self.state.text = waiting_panel_text(
                agent_name=creds.agent_name, query=broca_q
            )
            self.state.error = ""
            self.state.started_at = since
            self.state.linger_until = 0.0
        self._seek_agent = creds.agent_name
        self._seek_since = since
        self._notify()

        def runner() -> None:
            try:
                self._seek_and_stream(creds, since)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self.state.status = "error"
                    self.state.error = str(exc)[:200]
                    self.state.text = (self.state.text + f"\n[error] {exc}")[-8000:]
                    self.state.lingering = True
                    self.state.linger_until = time.time() + 15.0
                self._notify()

        self._thread = threading.Thread(
            target=runner, name=f"turn-stream-{creds.agent_name}", daemon=True
        )
        self._thread.start()

    def _set_query_and_waiting(self, *, query: str, run_id: str, agent_name: str) -> None:
        q = (query or "").strip()
        with self._lock:
            if q:
                self.state.query = q
                self.state.saw_waiting_query = True
            self.state.text = waiting_panel_text(
                agent_name=agent_name,
                run_id=run_id,
                query=self.state.query,
            )
            self.state.status = "streaming" if run_id else "seeking"
        self._notify()

    def _seek_and_stream(self, creds: LettaAgentCreds, since: float) -> None:
        deadline = since + self.SEEK_TIMEOUT_S
        run_id = None
        while time.time() < deadline and not self._stop_event.is_set():
            run_id = pick_run_id(
                creds, since_epoch=since, seen_run_ids=self._seen_runs
            )
            if run_id:
                break
            time.sleep(0.6)
        if self._stop_event.is_set():
            return
        if not run_id:
            with self._lock:
                self.state.status = "error"
                self.state.error = "no Letta run found"
                self.state.text = "Turn detected but no Letta run appeared in time."
                self.state.lingering = True
                self.state.linger_until = time.time() + 20.0
            self._notify()
            return

        self._seen_runs.add(run_id)
        # Bound memory of seen runs.
        if len(self._seen_runs) > 200:
            self._seen_runs = set(list(self._seen_runs)[-100:])

        with self._lock:
            self.state.run_id = run_id
            self.state.status = "streaming"
        # Show run-id waiting chrome immediately, then poll hard for the
        # triggering user query *before* the first model step so the popup
        # isn't blank while Letta thinks.
        self._set_query_and_waiting(
            query="", run_id=run_id, agent_name=creds.agent_name
        )
        query = ""
        early = ""
        wait_deadline = time.time() + 4.0
        while time.time() < wait_deadline and not self._stop_event.is_set():
            rows = fetch_run_messages(creds, run_id)
            q = extract_user_query(rows)
            early = messages_to_text(rows)
            if q and q != query:
                query = q
                self._set_query_and_waiting(
                    query=query, run_id=run_id, agent_name=creds.agent_name
                )
                # Dwell so the popup paints the ask before step text replaces it
                # (user_message and first step often arrive in the same poll).
                if not early:
                    time.sleep(0.15)
            if query and early:
                # Ensure at least one waiting+query frame before content.
                self._set_query_and_waiting(
                    query=query, run_id=run_id, agent_name=creds.agent_name
                )
                time.sleep(0.75)
                break
            if early and not query:
                # Steps landed before user_message is visible — keep polling
                # briefly for the ask so the waiting pane can still show it.
                time.sleep(0.1)
                continue
            if query:
                break
            time.sleep(0.1)
        if early:
            with self._lock:
                q = self.state.query or query
                self.state.text = with_query_header(early, q)[-12000:]
            self._notify()
        self._consume_stream(creds, run_id)

    def _consume_stream(self, creds: LettaAgentCreds, run_id: str) -> None:
        url = f"{creds.endpoint}/v1/runs/{run_id}/stream"
        body = json.dumps(
            {"starting_after": 0, "include_pings": True, "poll_interval": 0.5}
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {creds.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        buf_lines: list[str] = []
        stream_error = ""
        poll_stop = threading.Event()

        def _is_placeholder(cur: str) -> bool:
            c = (cur or "").strip()
            if c.startswith(("Streaming ", "Live on ", "Turn started")):
                return True
            if "waiting for first model step" in c.lower():
                return True
            if c.startswith(">") and (
                "Live on " in c
                or "Turn started" in c
                or "waiting for" in c.lower()
            ):
                return True
            return False

        def _apply_text(text: str) -> None:
            text = (text or "").strip()
            if not text:
                return
            with self._lock:
                cur = (self.state.text or "").strip()
                q = self.state.query
                # Capture query if poll only has user_message so far.
                if not q:
                    # messages_to_text skips user_message; poller may pass raw rows via side path
                    pass
                display = with_query_header(text, q)
                if _is_placeholder(cur) or len(display) >= len(cur):
                    self.state.text = display[-12000:]
                    self.state.status = "streaming"
            self._notify()

        def _poll_messages() -> None:
            """SSE is step-batched and blocks on readline — poll messages so UI
            updates as soon as a step lands, not only when the socket unblocks."""
            while not poll_stop.is_set() and not self._stop_event.is_set():
                try:
                    rows = fetch_run_messages(creds, run_id)
                    q = extract_user_query(rows)
                    msg_text = messages_to_text(rows)
                    have_steps = bool(msg_text)
                    if q:
                        with self._lock:
                            if not self.state.query:
                                self.state.query = q
                            need_waiting = not self.state.saw_waiting_query
                            cur = self.state.text
                        if need_waiting and (not have_steps or _is_placeholder(cur)):
                            self._set_query_and_waiting(
                                query=q,
                                run_id=run_id,
                                agent_name=creds.agent_name,
                            )
                            if have_steps:
                                time.sleep(0.5)
                        if have_steps:
                            _apply_text(msg_text)
                    elif msg_text:
                        _apply_text(msg_text)
                except Exception:
                    pass
                # Faster while still waiting so the query header lands before
                # the first step when Letta is slow to emit chunks.
                with self._lock:
                    still_wait = _is_placeholder(self.state.text)
                poll_stop.wait(0.2 if still_wait else 0.7)

        poller = threading.Thread(
            target=_poll_messages, name=f"turn-poll-{run_id[-8:]}", daemon=True
        )
        poller.start()
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                while not self._stop_event.is_set():
                    raw = resp.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        continue
                    if line.startswith(":"):
                        continue  # comment / ping
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    piece = format_stream_event(obj)
                    if not piece:
                        continue
                    # Step chunks: full reasoning/tool blocks as paragraphs.
                    if piece.startswith("[think]") or piece.startswith("[tool"):
                        buf_lines.append(piece)
                        buf_lines.append("")
                    else:
                        if buf_lines and not buf_lines[-1].startswith("["):
                            buf_lines[-1] = buf_lines[-1] + piece
                        else:
                            buf_lines.append(piece)
                    _apply_text("\n".join(buf_lines))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            stream_error = str(exc)[:200]
            with self._lock:
                self.state.error = stream_error
                if not self.state.text.strip() or _is_placeholder(self.state.text):
                    self.state.text = f"[stream error] {exc}"
            self._notify()
        finally:
            poll_stop.set()

        text_now = "\n".join(buf_lines).strip()
        if len(text_now) < 40:
            with self._lock:
                prior = (self.state.text or "").strip()
                q = self.state.query
            if prior and not _is_placeholder(prior) and not prior.startswith(
                "[stream error]"
            ):
                # Strip header for length check reuse
                text_now = prior.replace("\n\n— turn complete —", "").strip()
            if len(text_now) < 40:
                rows = fetch_run_messages(creds, run_id)
                if not q:
                    q = extract_user_query(rows)
                fallback = messages_to_text(rows)
                if fallback:
                    text_now = fallback

        with self._lock:
            q = self.state.query
            if text_now:
                self.state.text = with_query_header(text_now, q)[-12000:]
            elif _is_placeholder(self.state.text or ""):
                self.state.text = (
                    f"No stream content for {run_id}."
                    + (f" ({stream_error})" if stream_error else "")
                )
            final = (self.state.text or "").strip()
            thin = len(final) < 40
            self.state.status = "error" if (stream_error and thin) else "linger"
            self.state.lingering = True
            self.state.linger_until = time.time() + self.LINGER_S
            if final and not final.endswith("— turn complete —"):
                self.state.text = final + "\n\n— turn complete —"
        self._notify()

    def _notify(self) -> None:
        if self.on_update:
            try:
                self.on_update()
            except Exception:
                pass
