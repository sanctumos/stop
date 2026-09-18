"""Broca turn-start trap → Letta run SSE stream for the selected agent."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Broca console lines that mean a Letta turn just began.
_TURN_START_RES = [
    re.compile(p, re.I)
    for p in (
        r"Processing message in LIVE mode",
        r"Atomically dequeued message",
        r"Processing message with attached core block",
        r"HTTP Request: POST http://(?:localhost|127\.0\.0\.1):\d+/v1/agents/[^/\s]+/messages",
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
    old_lines = (prev or "").splitlines()
    new_lines = (new or "").splitlines()
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
    if mtype in ("ping", "keepalive"):
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
    """Fetch recent runs; prefer agent-scoped if supported, else filter client-side."""
    q = urllib.parse.urlencode({"limit": str(limit)})
    url = f"{creds.endpoint}/v1/runs/?{q}"
    try:
        data = _http_json("GET", url, api_key=creds.api_key, timeout=8.0)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
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


def pick_run_id(
    creds: LettaAgentCreds,
    *,
    since_epoch: float,
    seen_run_ids: set[str],
) -> str | None:
    """Choose a fresh background run for this agent after turn start."""
    # Active first.
    try:
        active = _http_json(
            "GET",
            f"{creds.endpoint}/v1/runs/active",
            api_key=creds.api_key,
            timeout=5.0,
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        active = []
    candidates: list[dict] = []
    if isinstance(active, list):
        candidates.extend(active)
    candidates.extend(list_runs_for_agent(creds, limit=20))

    best: tuple[float, str] | None = None
    # Accept runs created shortly before trap (clock skew / detect lag).
    floor = since_epoch - 30.0
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
        if created_epoch and created_epoch < floor:
            continue
        status = (r.get("status") or "").lower()
        # Rank: running/created > completed (still streamable for catch-up).
        rank = 2 if status in ("running", "created") else 1
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
                self.state.run_id = ""
                self.state.agent_name = ""
            self._notify()
            return

        if status in ("streaming", "seeking") or (
            self._thread and self._thread.is_alive()
        ):
            # Already on a turn.
            if selected_agent:
                self._prev_scroll[selected_agent] = broca_scrollback or ""
            return

        if not selected_agent:
            return

        prev = self._prev_scroll.get(selected_agent, "")
        new = broca_scrollback or ""
        self._prev_scroll[selected_agent] = new
        if not prev:
            # First paint — don't treat full history as a new turn.
            return
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
        with self._lock:
            self.state.active = True
            self.state.lingering = False
            self.state.agent_name = creds.agent_name
            self.state.run_id = ""
            self.state.status = "seeking"
            self.state.text = f"Turn started — waiting for Letta run ({creds.agent_name})…"
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
            self.state.text = f"Streaming {run_id}…\n"
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
                    # Token deltas: append; full reasoning blocks: new paragraph.
                    if piece.startswith("[think]") or piece.startswith("[tool"):
                        buf_lines.append(piece)
                        buf_lines.append("")
                    else:
                        if buf_lines and not buf_lines[-1].startswith("["):
                            buf_lines[-1] = buf_lines[-1] + piece
                        else:
                            buf_lines.append(piece)
                    text = "\n".join(buf_lines).strip()
                    with self._lock:
                        self.state.text = text[-12000:]
                        self.state.status = "streaming"
                    self._notify()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            with self._lock:
                self.state.status = "error"
                self.state.error = str(exc)[:200]
                if not self.state.text.strip():
                    self.state.text = f"[stream error] {exc}"
            self._notify()

        with self._lock:
            self.state.status = "linger"
            self.state.lingering = True
            self.state.linger_until = time.time() + self.LINGER_S
            if self.state.text.strip():
                self.state.text = self.state.text.rstrip() + "\n\n— turn complete —"
        self._notify()

    def _notify(self) -> None:
        if self.on_update:
            try:
                self.on_update()
            except Exception:
                pass
