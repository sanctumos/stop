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

from .http_safe import HttpPolicyError, http_json as _safe_http_json, urlopen as _safe_urlopen
from .http_safe import validate_outbound_url
from .livelog import unwrap_screen_hardcopy
from .telemetry import trace

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

# Leading timestamp on Broca / logging lines. Hardcopy reloads can resurface
# old LIVE-mode rows — those must not arm the turn popup.
_LOG_TS_RES = [
    re.compile(
        r"^\[?"
        r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
        r"\]?"
    ),
]

# Turn-start log line must be within this many seconds of wall clock.
TURN_START_MAX_AGE_S = 60.0

_TURN_END_BROCA_RES = [
    re.compile(p, re.I)
    for p in (
        r"Routing response through \w+ handler",
        r"Detaching core block",
    )
]


def parse_log_line_epoch(line: str) -> float | None:
    """Parse a leading log timestamp to epoch seconds, or None if absent/bad."""
    s = (line or "").strip()
    if not s:
        return None
    for rx in _LOG_TS_RES:
        m = rx.match(s)
        if not m:
            continue
        raw = m.group("ts").replace("T", " ")
        # Truncate fractional seconds for fromisoformat on 3.12+
        if "." in raw:
            main, frac = raw.split(".", 1)
            raw = f"{main}.{frac[:6]}"
        try:
            from datetime import datetime

            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            continue
    return None


def log_line_is_fresh(
    line: str,
    *,
    now: float | None = None,
    max_age_s: float = TURN_START_MAX_AGE_S,
) -> bool:
    """True when the line's timestamp is within ``max_age_s`` of ``now``.

    Untimestamped lines are rejected — reload thrash often re-surfaces
    stamp-less or ancient LIVE rows that are not real turns.
    """
    now = now if now is not None else time.time()
    epoch = parse_log_line_epoch(line)
    if epoch is None:
        return False
    return abs(now - epoch) <= max_age_s


def scrollback_signals_turn_start(
    prev: str,
    new: str,
    *,
    now: float | None = None,
    max_age_s: float = TURN_START_MAX_AGE_S,
) -> bool:
    """True when new Broca scrollback added a *fresh* turn-start line.

    Unstable hardcopy often rewrites the tail without a pure append. Falling
    back to ``new_lines[-12:]`` re-armed the popup at rest whenever those
    lines still contained an old ``LIVE mode`` row. Only lines that are not
    already in ``prev`` count — and the matching line's timestamp must be
    within about a minute of wall clock (reload/re-poll artifacts fail this).
    """
    if not (new or "").strip():
        return False
    now = now if now is not None else time.time()
    # hardcopy can embed NULs / C1 controls. Broca screens are 80 columns,
    # so "LIVE mode" arrives as "LIVE m" / "ode" until the lines are rejoined.
    prev = (prev or "").replace("\x00", "")
    new = (new or "").replace("\x00", "")
    old_lines = unwrap_screen_hardcopy(prev)
    new_lines = unwrap_screen_hardcopy(new)
    if new_lines == old_lines:
        return False
    if len(new_lines) >= len(old_lines) and new_lines[: len(old_lines)] == old_lines:
        delta = new_lines[len(old_lines) :]
    else:
        old_set = set(old_lines)
        delta = [ln for ln in new_lines if ln not in old_set]
        if not delta:
            return False
        delta = delta[-20:]
    for ln in delta:
        if not ln.strip():
            continue
        if not any(r.search(ln) for r in _TURN_START_RES):
            continue
        if log_line_is_fresh(ln, now=now, max_age_s=max_age_s):
            return True
    return False


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
    # When True with enabled, any Broca turn can own the popup and a newer
    # start preempts the current one (`f` follow). When False, only the
    # selected agent seeks (`t` selected).
    follow_all: bool = False
    active: bool = False
    lingering: bool = False
    agent_name: str = ""
    run_id: str = ""
    status: str = "idle"  # idle | seeking | streaming | linger | error | off
    text: str = ""
    query: str = ""  # triggering user message, shown while waiting / as header
    saw_waiting_query: bool = False  # True once waiting pane showed `> query`
    error: str = ""
    started_at: float = 0.0
    linger_until: float = 0.0
    # Background agents that signaled a turn while another turn is focused (#4069).
    pending_agents: list[str] = field(default_factory=list)


def _parse_env_file(path: Path) -> dict[str, str]:
    """KEY=value lines from one env file. Empty dict if missing/unreadable."""
    if not path.is_file():
        return {}
    vals: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            vals[k.strip()] = v.strip().strip("'").strip('"')
    except OSError:
        return {}
    return vals


# Only these keys are read from agent env files (never the whole dump).
_LETTA_ENV_KEYS = frozenset({"AGENT_ID", "AGENT_API_KEY", "AGENT_ENDPOINT"})


def load_agent_creds(agents_root: Path, agent_name: str) -> LettaAgentCreds | None:
    """Read Letta creds from agents/<name>/.env and/or broca/.env.

    Athena-style agents put AGENT_ID in ``broca/.env``. Q / Porter / Wren keep
    Letta identity one level up and only Broca/Telegram keys under broca/.
    Merge parent first, then broca (broca wins on overlap) so both layouts work.

    Does **not** fall back to ``LETTA_SERVER_PASSWORD`` — that is a host-wide
    secret and must not become a per-agent API key on multi-agent installs.
    """
    root = agents_root / agent_name
    raw = _parse_env_file(root / ".env")
    raw.update(_parse_env_file(root / "broca" / ".env"))
    if not raw:
        return None
    vals = {k: raw[k] for k in _LETTA_ENV_KEYS if k in raw}
    agent_id = vals.get("AGENT_ID") or ""
    api_key = vals.get("AGENT_API_KEY") or ""
    endpoint = (vals.get("AGENT_ENDPOINT") or "http://127.0.0.1:8284").rstrip("/")
    # AGENT_ENDPOINT sometimes includes /v1 — normalize to server root.
    if endpoint.endswith("/v1"):
        endpoint = endpoint[:-3]
    if not agent_id or not api_key:
        return None
    try:
        validate_outbound_url(endpoint)
    except HttpPolicyError:
        return None
    return LettaAgentCreds(
        agent_name=agent_name,
        agent_id=agent_id,
        api_key=api_key,
        endpoint=endpoint,
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


_BROCA_ENV_KEYS = frozenset({"OTTO_BRIDGE_HTTP_LISTEN", "OTTO_BRIDGE_HTTP_API_KEY"})


def broca_http_creds(agents_root: Path, agent_name: str) -> tuple[str, str] | None:
    """Return (base_url, api_key) for this agent's Otto bridge HTTP listener."""
    env_path = agents_root / agent_name / "broca" / ".env"
    if not env_path.is_file():
        env_path = agents_root / agent_name / ".env"
    if not env_path.is_file():
        return None
    raw = _parse_env_file(env_path)
    vals = {k: raw[k] for k in _BROCA_ENV_KEYS if k in raw}
    listen = (vals.get("OTTO_BRIDGE_HTTP_LISTEN") or "").strip()
    key = (vals.get("OTTO_BRIDGE_HTTP_API_KEY") or "").strip()
    if not listen or not key:
        return None
    if "://" in listen:
        base = listen.rstrip("/")
    else:
        host, _, port = listen.rpartition(":")
        host = host.strip() or "127.0.0.1"
        base = f"http://{host}:{port.strip()}"
    try:
        validate_outbound_url(base)
    except HttpPolicyError:
        return None
    return base, key


def fetch_broca_current_turn(
    agents_root: Path, agent_name: str
) -> dict:
    """Fetch the current Broca turn via its published HTTP API.

    Uses ``GET {OTTO_BRIDGE_HTTP_LISTEN}/v1/turn/current`` (published Broca Otto
    bridge API). Never opens the Broca SQLite database from stop (#4069).
    """
    creds = broca_http_creds(agents_root, agent_name)
    if creds is None:
        return {"active": False, "message": "", "status": "unavailable"}
    base, api_key = creds
    url = f"{base}/v1/turn/current"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with _safe_urlopen(req, timeout=3.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw.strip() else {}
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
        HttpPolicyError,
    ):
        return {"active": False, "message": "", "status": "unavailable"}
    if not isinstance(data, dict):
        return {"active": False, "message": "", "status": "invalid"}
    return {
        "active": bool(data.get("active")),
        "message": clean_user_query(str(data.get("message") or "")),
        "status": str(data.get("status") or ""),
        "queue_id": data.get("queue_id"),
    }


def fetch_broca_triggering_message(
    agents_root: Path, agent_name: str
) -> str:
    """Compatibility wrapper returning only the current query."""
    data = fetch_broca_current_turn(agents_root, agent_name)
    if not data.get("active"):
        return ""
    return str(data.get("message") or "")


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
            f"({agent_name} is thinking — SSE stays quiet until a step lands)"
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


def bounded_turn_text(body: str, query: str, *, max_chars: int = 12000) -> str:
    """Bound turn text without truncating the user-query header."""
    text = with_query_header(body, query)
    if len(text) <= max_chars:
        return text
    q = (query or "").strip()
    if not q:
        return text[-max_chars:]
    shown = q if len(q) <= 2000 else q[:2000] + "…"
    header = f"> {shown}\n\n"
    room = max(0, max_chars - len(header))
    return header + text[-room:] if room else header[:max_chars]


def _http_json(
    method: str,
    url: str,
    *,
    api_key: str,
    body: dict | None = None,
    timeout: float = 10.0,
) -> object:
    return _safe_http_json(
        method, url, api_key=api_key, body=body, timeout=timeout
    )


def list_runs_for_agent(
    creds: LettaAgentCreds, *, limit: int = 15
) -> list[dict]:
    """Fetch recent runs for this agent (server-side agent_id filter)."""
    q = urllib.parse.urlencode(
        {"limit": str(limit), "agent_id": creds.agent_id, "order": "desc"}
    )
    url = f"{creds.endpoint}/v1/runs/?{q}"
    try:
        data = _http_json("GET", url, api_key=creds.api_key, timeout=20.0)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        # Fallback without agent_id if older Letta rejects the param.
        try:
            q2 = urllib.parse.urlencode({"limit": str(max(limit, 50))})
            data = _http_json(
                "GET",
                f"{creds.endpoint}/v1/runs/?{q2}",
                api_key=creds.api_key,
                timeout=20.0,
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


def fetch_run_status(creds: LettaAgentCreds, run_id: str) -> str:
    """GET /v1/runs/{id} → lowercase status, or empty on failure."""
    url = f"{creds.endpoint}/v1/runs/{run_id}"
    try:
        data = _http_json("GET", url, api_key=creds.api_key, timeout=8.0)
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
    ):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("status") or "").lower()


def run_still_in_flight(status: str) -> bool:
    """True while Letta may still emit reasoning / assistant tokens."""
    return status in ("", "running", "created", "pending", "in_progress")


def fetch_run_messages(creds: LettaAgentCreds, run_id: str) -> list[dict]:
    """GET /v1/runs/{id}/messages — fallback when SSE yields nothing.

    While a Venice/minimax step is still generating, Letta often returns an
    empty list here even though the run is live. After each step completes,
    ``/v1/steps/{step_id}/messages`` is populated first — use that as a
    fallback so the popup is not blank until the whole run finishes.
    """
    url = f"{creds.endpoint}/v1/runs/{run_id}/messages?limit=100&order=asc"
    try:
        data = _http_json("GET", url, api_key=creds.api_key, timeout=12.0)
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
        HttpPolicyError,
    ):
        data = None
    rows: list[dict] = []
    if isinstance(data, list):
        rows = [x for x in data if isinstance(x, dict)]
    elif isinstance(data, dict):
        raw = data.get("messages") or data.get("data") or []
        rows = [x for x in raw if isinstance(x, dict)]
    if rows:
        return rows
    return _fetch_messages_via_steps(creds, run_id)


def _fetch_messages_via_steps(
    creds: LettaAgentCreds, run_id: str
) -> list[dict]:
    """Assemble messages from completed steps when the run messages list is empty."""
    url = f"{creds.endpoint}/v1/runs/{run_id}/steps"
    try:
        steps = _http_json("GET", url, api_key=creds.api_key, timeout=12.0)
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
        HttpPolicyError,
    ):
        return []
    if not isinstance(steps, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        sid = step.get("id") or ""
        if not sid:
            continue
        try:
            data = _http_json(
                "GET",
                f"{creds.endpoint}/v1/steps/{sid}/messages",
                api_key=creds.api_key,
                timeout=10.0,
            )
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
            HttpPolicyError,
        ):
            continue
        rows = data if isinstance(data, list) else []
        if isinstance(data, dict):
            rows = data.get("messages") or data.get("data") or []
        for obj in rows:
            if not isinstance(obj, dict):
                continue
            key = message_identity(obj)
            if key in seen:
                continue
            seen.add(key)
            out.append(obj)
    return out


def messages_to_text(rows: list[dict]) -> str:
    parts: list[str] = []
    for obj in rows:
        piece = format_stream_event(obj)
        if piece:
            parts.append(piece)
    return "\n\n".join(parts).strip()


def message_identity(obj: dict) -> str:
    """Stable identity for merge arbitration.

    Letta can reuse one message id for reasoning + assistant records in the
    same step, so message type is part of the identity. Deduping on id alone
    silently dropped the final assistant output after a thinking record.
    """
    mid = obj.get("id") or obj.get("message_id") or obj.get("ott_id")
    mtype = obj.get("message_type") or obj.get("type") or ""
    if mid is not None and str(mid).strip():
        return f"id:{mid}:{mtype}"
    piece = format_stream_event(obj)
    return f"fp:{mtype}:{hash(piece)}"


def ordered_unique_message_text(rows: list[dict]) -> str:
    """Poll path: one text block per message identity, first-seen order."""
    seen: set[str] = set()
    parts: list[str] = []
    for obj in rows:
        if not isinstance(obj, dict):
            continue
        piece = format_stream_event(obj)
        if not piece:
            continue
        key = message_identity(obj)
        if key in seen:
            continue
        seen.add(key)
        parts.append(piece)
    return "\n\n".join(parts).strip()


def merge_turn_bodies(*candidates: str) -> str:
    """Pick the best non-placeholder body without moving text backward.

    Prefer the longest candidate that extends (or equals) a shorter base.
    Identical content wins; pure length wars without shared prefix keep the
    longer string only when the shorter is a substring of the longer.
    """
    cleaned = [(c or "").strip() for c in candidates if (c or "").strip()]
    if not cleaned:
        return ""
    best = cleaned[0]
    for cand in cleaned[1:]:
        if cand == best:
            continue
        if best.startswith(cand) or cand.startswith(best):
            best = cand if len(cand) >= len(best) else best
            continue
        if best in cand:
            best = cand
            continue
        if cand in best:
            continue
        # Disjoint: keep longer (poll usually wins completeness).
        if len(cand) > len(best):
            best = cand
    return best


def prune_seen_runs(order: list[str], *, maxlen: int = 100) -> tuple[list[str], set[str]]:
    """Deterministic prune: keep the newest ``maxlen`` run ids."""
    if len(order) <= maxlen:
        return order, set(order)
    kept = order[-maxlen:]
    return kept, set(kept)


def normalized_query(text: str) -> str:
    """Canonical query text used to correlate Broca and Letta records."""
    return " ".join(clean_user_query(text).split())


def run_query_matches(rows: list[dict], expected_query: str) -> bool:
    """True only when this run contains the current Broca user query."""
    expected = normalized_query(expected_query)
    actual = normalized_query(extract_user_query(rows))
    return bool(expected and actual and expected == actual)


def extend_seek_deadline(
    *,
    since: float,
    now: float,
    deadline: float,
    broca_active: bool,
    max_s: float,
    have_query: bool = False,
) -> float:
    """Keep seeking while Broca still owns the turn, up to ``max_s``.

    A flat 45s seek expires while Letta is still inside the blocking
    messages POST (these turns run 50–240s). If the run row is not
    listable until that POST returns, the popup times out on the query.

    When the log edge already gave us the Broca text, keep looking even
    if ``/v1/turn/current`` has flipped idle — that probe can lag or go
    idle between dequeue and the Letta POST.
    """
    if not broca_active and not have_query:
        return deadline
    return min(since + max_s, max(deadline, now + 3.0))


def pick_run_id(
    creds: LettaAgentCreds,
    *,
    since_epoch: float,
    seen_run_ids: set[str],
    recovery_query: str = "",
    now_epoch: float | None = None,
    max_recovery_age_s: float = 900.0,
) -> str | None:
    """Choose a fresh run, correlating it to the Broca query when we have one.

    Letta's ``/runs/active`` may include zombie rows many months old. A normal
    log-edge seek keeps a 3-minute creation window. Completed runs are only
    that wide when Broca's query is known — otherwise an 8-second catch-up
    so the previous turn cannot steal the pane. A matching finished run from
    this turn must still be selectable after Letta's blocking POST returns.
    """
    now_epoch = now_epoch or time.time()
    has_query = bool(normalized_query(recovery_query))
    http_error = ""
    active: object = []
    try:
        q = urllib.parse.urlencode({"agent_id": creds.agent_id})
        active = _http_json(
            "GET",
            f"{creds.endpoint}/v1/runs/active?{q}",
            api_key=creds.api_key,
            timeout=20.0,
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        http_error = type(exc).__name__
        try:
            active = _http_json(
                "GET",
                f"{creds.endpoint}/v1/runs/active",
                api_key=creds.api_key,
                timeout=20.0,
            )
            http_error = ""
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            json.JSONDecodeError,
        ) as exc2:
            http_error = type(exc2).__name__
            active = []
    candidates: list[dict] = []
    if isinstance(active, list):
        candidates.extend(active)
    listed = list_runs_for_agent(creds, limit=25)
    candidates.extend(listed)

    considered: set[str] = set()
    tight_floor = since_epoch - 180.0
    wide_floor = now_epoch - max_recovery_age_s
    live_floor = min(tight_floor, wide_floor) if has_query else tight_floor
    completed_floor = wide_floor if has_query else since_epoch - 8.0
    scored: list[tuple[float, str, bool]] = []
    counts = {
        "old": 0,
        "other_agent": 0,
        "seen": 0,
        "foreground": 0,
        "status": 0,
        "query": 0,
    }
    near: list[dict] = []

    def _note(why: str, rid: str, created_epoch: float = 0.0, **extra: object) -> None:
        counts[why] = counts.get(why, 0) + 1
        # Zombies and already-seen rows are counts only. Near misses are
        # the rows that could have been this turn.
        if why in ("old", "other_agent", "seen") or len(near) >= 8:
            return
        age = None
        if created_epoch:
            age = int((now_epoch - created_epoch) // 30) * 30
        row: dict = {"id": (rid or "")[-12:], "why": why, "age_s": age}
        row.update(extra)
        near.append(row)

    for r in candidates:
        if not isinstance(r, dict):
            continue
        if r.get("agent_id") != creds.agent_id:
            _note("other_agent", str(r.get("id") or ""))
            continue
        rid = r.get("id") or ""
        if not rid or rid in seen_run_ids or rid in considered:
            _note("seen", str(rid))
            continue
        considered.add(rid)
        created_epoch = _parse_iso(r.get("created_at") or "")
        status = (r.get("status") or "").lower()
        if r.get("background") is False:
            _note("foreground", rid, created_epoch, status=status)
            continue
        if status in ("running", "created"):
            if not created_epoch or created_epoch < live_floor:
                _note("old", rid, created_epoch, status=status)
                continue
            rank = 2
        elif status in ("completed", "succeeded"):
            if not created_epoch or created_epoch < completed_floor:
                _note("old", rid, created_epoch, status=status)
                continue
            rank = 1
        else:
            _note("status", rid, created_epoch, status=status or "?")
            continue
        matched = False
        if has_query:
            matched = run_query_matches(
                fetch_run_messages(creds, rid), recovery_query
            )
            if status in ("completed", "succeeded") and not matched:
                _note("query", rid, created_epoch, status=status)
                continue
            if (
                status in ("running", "created")
                and created_epoch < tight_floor
                and not matched
            ):
                _note("query", rid, created_epoch, status=status)
                continue
        score = rank * 1e12 + (created_epoch or 0.0)
        scored.append((score, rid, matched))
    if has_query and any(row[2] for row in scored):
        scored = [row for row in scored if row[2]]
    scored.sort(reverse=True)
    chosen = scored[0][1] if scored else ""
    trace(
        "turn",
        "pick",
        agent=creds.agent_name,
        chosen=(chosen or "")[-12:],
        has_query=has_query,
        active=len(active) if isinstance(active, list) else -1,
        listed=len(listed),
        http=http_error,
        counts=counts,
        near=near,
    )
    return chosen or None


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
    # Broca stays "processing" for the whole Letta turn. Keep looking that
    # long instead of giving up at 45s with the user query still on screen.
    SEEK_MAX_S = 300.0
    # After linger clears, ignore turn traps briefly — unstable hardcopy still
    # carries old LIVE-mode lines and was re-popping the query pane at rest.
    RETRIGGER_COOLDOWN_S = 12.0
    # Per-socket read idle. Long thinking sends no tokens — reconnect, do not
    # treat this as turn complete.
    SSE_READ_TIMEOUT_S = 45.0
    # Hard wall for one turn (seek+stream). Prevents forever-open SSE loops.
    STREAM_WALL_S = 900.0
    SEEN_RUNS_MAX = 100
    # Must exceed SSE_READ_TIMEOUT_S once the socket is closed on disable;
    # close-on-disable unblocks readline so joins finish well under this.
    DISABLE_JOIN_S = 5.0
    SHUTDOWN_JOIN_S = 5.0

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
        self._seen_run_order: list[str] = []
        self._seen_runs: set[str] = set()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._seek_agent: str | None = None
        self._seek_since: float = 0.0
        self._cooldown_until: dict[str, float] = {}
        self._generation = 0
        self._pending_agents: list[str] = []
        self._followup_agent: str | None = None
        self._selected_agent: str | None = None
        self._probe_thread: threading.Thread | None = None
        self._probe_after = 0.0
        self._seen_bridge_turns: set[tuple[str, object]] = set()
        self._active_response = None
        self._aux_threads: list[threading.Thread] = []
        self._shutting_down = False
        self._armed_queue_id: object | None = None

    def _register_aux(self, thread: threading.Thread) -> None:
        with self._lock:
            self._aux_threads = [t for t in self._aux_threads if t.is_alive()]
            self._aux_threads.append(thread)

    def _close_active_stream(self) -> None:
        resp = None
        with self._lock:
            resp = self._active_response
            self._active_response = None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def _set_active_response(self, resp) -> None:  # noqa: ANN001
        with self._lock:
            self._active_response = resp

    def _join_workers(self, *, timeout: float) -> None:
        deadline = time.time() + timeout
        threads: list[threading.Thread] = []
        with self._lock:
            if self._thread is not None:
                threads.append(self._thread)
            if self._probe_thread is not None:
                threads.append(self._probe_thread)
            threads.extend(self._aux_threads)
        for t in threads:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if t.is_alive():
                t.join(timeout=remaining)

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Hard stop for TUI quit — clear callbacks, cancel streams, join threads."""
        budget = self.SHUTDOWN_JOIN_S if timeout is None else timeout
        self.on_update = None
        with self._lock:
            self._shutting_down = True
        self.set_enabled(False)
        self._close_active_stream()
        self._join_workers(timeout=budget)
        with self._lock:
            self._thread = None
            self._probe_thread = None
            self._aux_threads = []

    def _gen_ok(self, gen: int) -> bool:
        with self._lock:
            return (
                self.state.enabled
                and not self._shutting_down
                and gen == self._generation
            )

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            was = self.state.enabled
            # Always bump generation so in-flight workers go stale.
            self._generation += 1
            if not enabled:
                self._stop_event.set()
                self.state.enabled = False
                self.state.follow_all = False
                self.state.active = False
                self.state.lingering = False
                self.state.status = "off"
                self.state.text = ""
                self.state.query = ""
                self.state.saw_waiting_query = False
                self.state.error = ""
                self.state.run_id = ""
                self.state.pending_agents = []
                self._seek_agent = None
                self._pending_agents = []
                self._followup_agent = None
                self._armed_queue_id = None
            else:
                self._stop_event.clear()
                self.state.enabled = True
                self._shutting_down = False
                if not was:
                    self.state.follow_all = False
                    self.state.active = False
                    self.state.lingering = False
                    self.state.status = "idle"
                    self.state.text = ""
                    self.state.query = ""
                    self.state.saw_waiting_query = False
                    self.state.error = ""
                    self.state.run_id = ""
                    self.state.pending_agents = []
                    self._seek_agent = None
                    self._pending_agents = []
                    self._followup_agent = None
        if not enabled:
            self._close_active_stream()
            self._join_workers(timeout=self.DISABLE_JOIN_S)

    def set_mode(self, mode: str) -> None:
        """``off`` | ``selected`` | ``follow``. ``t`` and ``f`` map here."""
        if mode == "off":
            self.set_enabled(False)
            return
        if mode == "follow":
            self.set_enabled(True)
            with self._lock:
                self.state.follow_all = True
                if self.state.status == "off":
                    self.state.status = "idle"
            return
        # selected
        self.set_enabled(True)
        with self._lock:
            self.state.follow_all = False
            if self.state.status == "off":
                self.state.status = "idle"

    def toggle(self) -> bool:
        """``t`` — selected-agent turn stream. Turns follow off."""
        with self._lock:
            selected_on = self.state.enabled and not self.state.follow_all
        if selected_on:
            self.set_mode("off")
            return False
        self.set_mode("selected")
        return True

    def toggle_follow(self) -> bool:
        """``f`` — any-agent turn stream with preempt. Turns selected mode off."""
        with self._lock:
            follow_on = self.state.enabled and self.state.follow_all
        if follow_on:
            self.set_mode("off")
            return False
        self.set_mode("follow")
        return True

    def snapshot(self) -> TurnStreamState:
        with self._lock:
            s = self.state
            return TurnStreamState(
                enabled=s.enabled,
                follow_all=s.follow_all,
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
                pending_agents=list(self._pending_agents),
            )

    def tick(
        self,
        *,
        selected_agent: str | None,
        broca_scrollback: str = "",
        broca_by_agent: dict[str, str] | None = None,
        now: float | None = None,
    ) -> None:
        """Observe Broca scrollbacks and arm the turn popup.

        Selected mode (``t``): seek only for the focused agent; other starts
        become ``pending_agents`` badges and never replace an in-flight turn.

        Follow mode (``f``): any agent can own the popup; a newer start
        preempts the current stream.
        """
        now = now or time.time()
        by_agent: dict[str, str] = {}
        if broca_by_agent:
            by_agent.update(broca_by_agent)
        if selected_agent is not None and selected_agent not in by_agent:
            by_agent[selected_agent] = broca_scrollback or ""

        with self._lock:
            self._selected_agent = selected_agent
            enabled = self.state.enabled
            follow_all = self.state.follow_all
            lingering = self.state.lingering
            linger_until = self.state.linger_until
            status = self.state.status
            current_agent = self.state.agent_name
            thread_alive = bool(self._thread and self._thread.is_alive())

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
                if selected_agent:
                    self._cooldown_until[selected_agent] = (
                        now + self.RETRIGGER_COOLDOWN_S
                    )
                for name, text in by_agent.items():
                    if (text or "").strip():
                        self._prev_scroll[name] = text
            self._notify()
            return

        busy = (
            lingering
            or status in ("streaming", "seeking", "linger", "error")
            or thread_alive
        )

        # Advance cursors / detect starts under the same lock as cooldowns.
        started: list[str] = []
        with self._lock:
            for name, new in sorted(by_agent.items()):
                if not (new or "").strip():
                    continue
                if now < self._cooldown_until.get(name, 0.0):
                    self._prev_scroll[name] = new
                    continue
                prev = self._prev_scroll.get(name)
                if prev is None:
                    self._prev_scroll[name] = new
                    if scrollback_signals_turn_start("", new, now=now):
                        started.append(name)
                    continue
                self._prev_scroll[name] = new
                if scrollback_signals_turn_start(prev, new, now=now):
                    started.append(name)

        if follow_all:
            self._tick_follow(
                started=started,
                busy=busy,
                status=status,
                current_agent=current_agent,
                now=now,
            )
            return

        # --- selected mode (t) below ---

        # A finished popup may still be on screen. The next real turn for
        # the agent you are watching has to replace it, or that turn is lost.
        if busy and selected_agent in started and status == "linger":
            with self._lock:
                self.state.lingering = False
                self.state.active = False
                self.state.status = "idle"
                self.state.text = ""
                self.state.query = ""
                self.state.saw_waiting_query = False
                self.state.error = ""
                self.state.run_id = ""
            busy = False

        if busy:
            # Queue background starts; never interrupt an in-flight focused turn.
            # Do NOT arm follow-up from hardcopy LIVE edges while seeking/
            # streaming — unstable hardcopy was wiping the finished answer
            # with a queryless re-seek the instant stream_end fired
            # (Athena 2026-09-23). Real next turns land via linger→LIVE or
            # the idle Broca probe.
            changed = False
            with self._lock:
                for name in started:
                    if name == selected_agent:
                        continue
                    if name not in self._pending_agents:
                        self._pending_agents.append(name)
                        changed = True
                if changed:
                    self.state.pending_agents = list(self._pending_agents)
            if changed:
                self._notify()
            return

        if not selected_agent:
            # Still record pending badges when nothing is selected.
            with self._lock:
                for name in started:
                    if name not in self._pending_agents:
                        self._pending_agents.append(name)
                        self.state.pending_agents = list(self._pending_agents)
            return

        # Prefer selected agent's start; else leave others as pending.
        if selected_agent in started:
            with self._lock:
                for name in started:
                    if name != selected_agent and name not in self._pending_agents:
                        self._pending_agents.append(name)
                pending = list(self._pending_agents)
            creds = load_agent_creds(self.agents_root, selected_agent)
            if creds is None:
                with self._lock:
                    self.state.status = "error"
                    self.state.error = f"no Letta creds for {selected_agent}"
                    self.state.active = True
                    self.state.agent_name = selected_agent
                    self.state.linger_until = now + 8.0
                    self.state.lingering = True
                    self.state.pending_agents = pending
                self._notify()
                return
            self._start_seek(creds, since=now)
            return

        # Recover turns already in progress when stop starts/reloads after the
        # LIVE log edge. The HTTP probe runs off the UI thread.
        self._probe_current_turn(selected_agent, now=now)

        with self._lock:
            changed = False
            for name in started:
                if name not in self._pending_agents:
                    self._pending_agents.append(name)
                    changed = True
            if changed:
                self.state.pending_agents = list(self._pending_agents)
        if changed:
            self._notify()

    def _tick_follow(
        self,
        *,
        started: list[str],
        busy: bool,
        status: str,
        current_agent: str,
        now: float,
    ) -> None:
        """Any-agent popup; a newer start drops the current turn."""
        if not started:
            if not busy:
                # Idle follow still recovers an already-running selected turn.
                if self._selected_agent:
                    self._probe_current_turn(self._selected_agent, now=now)
            return

        # Prefer a start that is not the one already on screen; otherwise take
        # the last detected (stable sort by agent name, then walk).
        target = None
        for name in started:
            if name != current_agent:
                target = name
                break
        if target is None:
            target = started[-1]

        if busy and status == "linger":
            with self._lock:
                self.state.lingering = False
                self.state.active = False
                self.state.status = "idle"
                self.state.text = ""
                self.state.query = ""
                self.state.saw_waiting_query = False
                self.state.error = ""
                self.state.run_id = ""
            busy = False

        if busy:
            # Preempt: drop the current stream and switch to the new start.
            self._preempt_and_seek(
                target, since=now, from_agent=current_agent or ""
            )
            return

        self._begin_agent_seek(target, since=now)

    def _begin_agent_seek(self, agent_name: str, *, since: float) -> None:
        creds = load_agent_creds(self.agents_root, agent_name)
        if creds is None:
            with self._lock:
                self.state.status = "error"
                self.state.error = f"no Letta creds for {agent_name}"
                self.state.active = True
                self.state.agent_name = agent_name
                self.state.linger_until = since + 8.0
                self.state.lingering = True
            self._notify()
            return
        self._start_seek(creds, since=since)

    def _preempt_and_seek(
        self, agent_name: str, *, since: float, from_agent: str = ""
    ) -> None:
        """Abort the in-flight turn and start seeking ``agent_name``."""
        thread: threading.Thread | None = None
        with self._lock:
            self._generation += 1
            self._stop_event.set()
            self._followup_agent = None
            self.state.active = False
            self.state.lingering = False
            self.state.status = "idle"
            self.state.text = ""
            self.state.query = ""
            self.state.saw_waiting_query = False
            self.state.error = ""
            self.state.run_id = ""
            self.state.agent_name = ""
            self.state.pending_agents = []
            self._pending_agents = []
            thread = self._thread
        self._close_active_stream()
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.8)
        self._stop_event.clear()
        trace(
            "turn",
            "preempt",
            from_agent=from_agent,
            to_agent=agent_name,
        )
        self._begin_agent_seek(agent_name, since=since)

    def _probe_current_turn(self, agent_name: str, *, now: float) -> None:
        if now < self._probe_after:
            return
        if self._probe_thread is not None and self._probe_thread.is_alive():
            return
        self._probe_after = now + 1.5

        def probe() -> None:
            data = fetch_broca_current_turn(self.agents_root, agent_name)
            if not data.get("active") or self._selected_agent != agent_name:
                return
            turn_key = (agent_name, data.get("queue_id") or data.get("message"))
            with self._lock:
                idle = (
                    self.state.enabled
                    and not self.state.active
                    and self.state.status == "idle"
                    and turn_key not in self._seen_bridge_turns
                )
                if idle:
                    self._seen_bridge_turns.add(turn_key)
                    if len(self._seen_bridge_turns) > 100:
                        self._seen_bridge_turns = set(list(self._seen_bridge_turns)[-50:])
            if not idle:
                return
            creds = load_agent_creds(self.agents_root, agent_name)
            if creds is None:
                self._set_error(agent_name, "no Letta creds for turn stream")
                return
            self._start_seek(
                creds,
                since=time.time(),
                initial_query=str(data.get("message") or ""),
                recovery_query=str(data.get("message") or ""),
            )

        self._probe_thread = threading.Thread(
            target=probe, name=f"stop-turn-probe-{agent_name}", daemon=True
        )
        self._probe_thread.start()

    def _start_seek(
        self,
        creds: LettaAgentCreds,
        *,
        since: float,
        initial_query: str = "",
        recovery_query: str = "",
    ) -> None:
        self._stop_event.clear()
        broca_q = clean_user_query(initial_query)
        with self._lock:
            # A log edge and the recovery probe can land together.
            if self.state.active or self.state.status != "idle":
                return
            self._generation += 1
            gen = self._generation
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
        trace(
            "turn",
            "seek_start",
            agent=creds.agent_name,
            has_query=bool(broca_q or recovery_query),
            query_len=len(broca_q or recovery_query or ""),
        )
        self._notify()

        def runner() -> None:
            try:
                turn0 = fetch_broca_current_turn(
                    self.agents_root, creds.agent_name
                )
                with self._lock:
                    self._armed_queue_id = turn0.get("queue_id")
                resolved = clean_user_query(recovery_query) or broca_q
                if not resolved:
                    query = fetch_broca_triggering_message(
                        self.agents_root, creds.agent_name
                    )
                    if query:
                        resolved = query
                        self._set_query_and_waiting(
                            query=query,
                            run_id="",
                            agent_name=creds.agent_name,
                            gen=gen,
                        )
                self._seek_and_stream(
                    creds,
                    since,
                    gen,
                    recovery_query=resolved,
                )
            except Exception as exc:  # noqa: BLE001
                if not self._gen_ok(gen):
                    return
                trace(
                    "turn",
                    "seek_error",
                    agent=creds.agent_name,
                    error=type(exc).__name__,
                    detail=str(exc)[:160],
                )
                with self._lock:
                    if gen != self._generation or not self.state.enabled:
                        return
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

    def _set_query_and_waiting(
        self, *, query: str, run_id: str, agent_name: str, gen: int
    ) -> None:
        if not self._gen_ok(gen):
            return
        q = (query or "").strip()
        with self._lock:
            if gen != self._generation or not self.state.enabled:
                return
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

    def _remember_run(self, run_id: str) -> None:
        if run_id in self._seen_runs:
            return
        self._seen_run_order.append(run_id)
        self._seen_run_order, self._seen_runs = prune_seen_runs(
            self._seen_run_order, maxlen=self.SEEN_RUNS_MAX
        )

    def _seek_and_stream(
        self,
        creds: LettaAgentCreds,
        since: float,
        gen: int,
        *,
        recovery_query: str = "",
    ) -> None:
        deadline = since + self.SEEK_TIMEOUT_S
        run_id = None
        while time.time() < deadline and not self._stop_event.is_set():
            if not self._gen_ok(gen):
                return
            run_id = pick_run_id(
                creds,
                since_epoch=since,
                seen_run_ids=self._seen_runs,
                recovery_query=recovery_query,
            )
            if run_id:
                break
            turn = fetch_broca_current_turn(self.agents_root, creds.agent_name)
            new_deadline = extend_seek_deadline(
                since=since,
                now=time.time(),
                deadline=deadline,
                broca_active=bool(turn.get("active")),
                max_s=self.SEEK_MAX_S,
                have_query=bool(normalized_query(recovery_query)),
            )
            if new_deadline != deadline:
                trace(
                    "turn",
                    "seek_extend",
                    agent=creds.agent_name,
                    broca_active=bool(turn.get("active")),
                    extra_s=int(new_deadline - since),
                )
            deadline = new_deadline
            time.sleep(0.6)
        if self._stop_event.is_set() or not self._gen_ok(gen):
            return
        if not run_id:
            with self._lock:
                if gen != self._generation or not self.state.enabled:
                    return
                self.state.status = "error"
                self.state.error = "no Letta run found"
                self.state.text = with_query_header(
                    "Turn detected but no Letta run appeared in time.",
                    self.state.query,
                )
                self.state.lingering = True
                self.state.linger_until = time.time() + 20.0
            trace(
                "turn",
                "seek_give_up",
                agent=creds.agent_name,
                waited_s=int(time.time() - since),
                query_len=len(recovery_query or ""),
            )
            self._notify()
            return

        self._remember_run(run_id)

        with self._lock:
            if gen != self._generation or not self.state.enabled:
                return
            self.state.run_id = run_id
            self.state.status = "streaming"
        # Show run-id waiting chrome immediately, then poll hard for the
        # triggering user query *before* the first model step so the popup
        # isn't blank while Letta thinks.
        self._set_query_and_waiting(
            query="", run_id=run_id, agent_name=creds.agent_name, gen=gen
        )
        query = ""
        early = ""
        wait_deadline = time.time() + 4.0
        while time.time() < wait_deadline and not self._stop_event.is_set():
            if not self._gen_ok(gen):
                return
            rows = fetch_run_messages(creds, run_id)
            q = extract_user_query(rows)
            early = ordered_unique_message_text(rows)
            if q and q != query:
                query = q
                self._set_query_and_waiting(
                    query=query, run_id=run_id, agent_name=creds.agent_name, gen=gen
                )
                # Dwell so the popup paints the ask before step text replaces it
                # (user_message and first step often arrive in the same poll).
                if not early:
                    time.sleep(0.15)
            if query and early:
                # Ensure at least one waiting+query frame before content.
                self._set_query_and_waiting(
                    query=query, run_id=run_id, agent_name=creds.agent_name, gen=gen
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
        if early and self._gen_ok(gen):
            with self._lock:
                if gen != self._generation or not self.state.enabled:
                    return
                q = self.state.query or query
                self.state.text = with_query_header(early, q)[-12000:]
            self._notify()
        self._consume_stream(creds, run_id, gen)

    def _consume_stream(
        self, creds: LettaAgentCreds, run_id: str, gen: int
    ) -> None:
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
        known_ids: set[str] = set()
        trace("turn", "stream_open", agent=creds.agent_name, run=(run_id or "")[-12:])

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
            if not self._gen_ok(gen):
                return
            changed = False
            with self._lock:
                if gen != self._generation or not self.state.enabled:
                    return
                cur = (self.state.text or "").strip()
                q = self.state.query
                display = with_query_header(text, q)
                if _is_placeholder(cur):
                    if display != self.state.text:
                        self.state.text = display[-12000:]
                        self.state.status = "streaming"
                        changed = True
                else:
                    merged = merge_turn_bodies(cur, display)
                    # Never move backward to a shorter unrelated body.
                    if merged != cur and (
                        len(merged) >= len(cur)
                        or _is_placeholder(cur)
                        or cur in merged
                    ):
                        if merged != self.state.text:
                            self.state.text = merged[-12000:]
                            self.state.status = "streaming"
                            changed = True
            if changed:
                self._notify()

        def _poll_messages() -> None:
            """SSE is step-batched and blocks on readline — poll messages so UI
            updates as soon as a step lands, not only when the socket unblocks."""
            while not poll_stop.is_set() and not self._stop_event.is_set():
                if not self._gen_ok(gen):
                    return
                try:
                    rows = fetch_run_messages(creds, run_id)
                    q = extract_user_query(rows)
                    msg_text = ordered_unique_message_text(rows)
                    for obj in rows:
                        if isinstance(obj, dict):
                            known_ids.add(message_identity(obj))
                    have_steps = bool(msg_text)
                    if q:
                        with self._lock:
                            if gen != self._generation or not self.state.enabled:
                                return
                            if not self.state.query:
                                self.state.query = q
                            need_waiting = not self.state.saw_waiting_query
                            cur = self.state.text
                        if need_waiting and (not have_steps or _is_placeholder(cur)):
                            self._set_query_and_waiting(
                                query=q,
                                run_id=run_id,
                                agent_name=creds.agent_name,
                                gen=gen,
                            )
                            if have_steps:
                                time.sleep(0.5)
                        if have_steps:
                            _apply_text(msg_text)
                    elif msg_text:
                        _apply_text(msg_text)
                except (
                    urllib.error.URLError,
                    urllib.error.HTTPError,
                    TimeoutError,
                    OSError,
                    json.JSONDecodeError,
                    HttpPolicyError,
                ) as exc:
                    trace(
                        "turn",
                        "poll_error",
                        agent=creds.agent_name,
                        run=(run_id or "")[-12:],
                        error=type(exc).__name__,
                        detail=str(exc)[:160],
                    )
                    with self._lock:
                        if gen == self._generation and self.state.enabled:
                            # Sticky surface for control-plane poll failures.
                            if not self.state.error:
                                self.state.error = f"message poll: {type(exc).__name__}"
                    self._notify()
                # Faster while still waiting so the query header lands before
                # the first step when Letta is slow to emit chunks.
                with self._lock:
                    still_wait = _is_placeholder(self.state.text)
                poll_stop.wait(0.2 if still_wait else 0.7)

        poller = threading.Thread(
            target=_poll_messages, name=f"turn-poll-{run_id[-8:]}", daemon=True
        )
        self._register_aux(poller)
        poller.start()
        stream_done = False
        wall_deadline = time.time() + self.STREAM_WALL_S
        try:
            # Bounded socket wait; reconnect while the Letta run is still live.
            # Idle SSE timeouts during thinking must NOT end the turn.
            while (
                not stream_done
                and not self._stop_event.is_set()
                and self._gen_ok(gen)
            ):
                if time.time() >= wall_deadline:
                    stream_error = stream_error or "stream wall clock exceeded"
                    break
                try:
                    resp = _safe_urlopen(req, timeout=self.SSE_READ_TIMEOUT_S)
                    self._set_active_response(resp)
                    try:
                        while not self._stop_event.is_set() and self._gen_ok(gen):
                            if time.time() >= wall_deadline:
                                stream_error = stream_error or (
                                    "stream wall clock exceeded"
                                )
                                stream_done = True
                                break
                            raw = resp.readline()
                            if not raw:
                                # EOF — only terminal if the run finished.
                                status = fetch_run_status(creds, run_id)
                                if run_still_in_flight(status):
                                    # Keep waiting chrome so the overlay does
                                    # not blank during long reasoning gaps.
                                    with self._lock:
                                        if (
                                            gen == self._generation
                                            and self.state.enabled
                                            and _is_placeholder(self.state.text)
                                        ):
                                            self.state.status = "streaming"
                                    self._notify()
                                    time.sleep(0.4)
                                    break  # reconnect outer loop
                                stream_done = True
                                break
                            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                            if not line or line.startswith(":"):
                                continue
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                # Confirm run actually finished — Letta may
                                # close the observer while still thinking.
                                status = fetch_run_status(creds, run_id)
                                if run_still_in_flight(status):
                                    time.sleep(0.4)
                                    break  # reconnect
                                stream_done = True
                                break
                            try:
                                obj = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(obj, dict):
                                continue
                            mid = message_identity(obj)
                            piece = format_stream_event(obj)
                            if not piece:
                                continue
                            # Poll already delivered this message — skip SSE echo.
                            if mid in known_ids and mid.startswith("id:"):
                                continue
                            known_ids.add(mid)
                            if piece.startswith("[think]") or piece.startswith("[tool"):
                                buf_lines.append(piece)
                                buf_lines.append("")
                            else:
                                if buf_lines and not buf_lines[-1].startswith("["):
                                    buf_lines[-1] = buf_lines[-1] + piece
                                else:
                                    buf_lines.append(piece)
                            _apply_text("\n".join(buf_lines))
                    finally:
                        self._close_active_stream()
                except (
                    urllib.error.URLError,
                    urllib.error.HTTPError,
                    TimeoutError,
                    OSError,
                    HttpPolicyError,
                ) as exc:
                    stream_error = str(exc)[:200]
                    err_l = stream_error.lower()
                    soft = (
                        "timed out" in err_l
                        or "timeout" in err_l
                        or isinstance(exc, TimeoutError)
                    )
                    trace(
                        "turn",
                        "stream_idle" if soft else "stream_error",
                        agent=creds.agent_name,
                        run=(run_id or "")[-12:],
                        error=type(exc).__name__,
                        detail=stream_error,
                    )
                    if self._stop_event.is_set() or not self._gen_ok(gen):
                        break
                    status = fetch_run_status(creds, run_id)
                    # Idle SSE timeouts during thinking are soft. Once the
                    # run is finished, stop reconnecting — otherwise the
                    # overlay never reaches stream_end / linger and the next
                    # turn has to fight a stuck worker (Athena 2026-09-23).
                    if soft and run_still_in_flight(status):
                        with self._lock:
                            if gen == self._generation and self.state.enabled:
                                self.state.status = "streaming"
                                if _is_placeholder(self.state.text) or not (
                                    self.state.text or ""
                                ).strip():
                                    self.state.text = waiting_panel_text(
                                        agent_name=creds.agent_name,
                                        run_id=run_id,
                                        query=self.state.query,
                                    )
                        self._notify()
                        stream_error = ""
                        time.sleep(0.3)
                        continue
                    if soft and not run_still_in_flight(status):
                        stream_error = ""
                        stream_done = True
                        break
                    if run_still_in_flight(status):
                        with self._lock:
                            if gen == self._generation and self.state.enabled:
                                self.state.status = "streaming"
                                if _is_placeholder(self.state.text) or not (
                                    self.state.text or ""
                                ).strip():
                                    self.state.text = waiting_panel_text(
                                        agent_name=creds.agent_name,
                                        run_id=run_id,
                                        query=self.state.query,
                                    )
                        self._notify()
                        time.sleep(0.3)
                        continue
                    with self._lock:
                        if gen != self._generation or not self.state.enabled:
                            break
                        self.state.error = stream_error
                        if not self.state.text.strip() or _is_placeholder(
                            self.state.text
                        ):
                            self.state.text = f"[stream error] {exc}"
                    self._notify()
                    break
        finally:
            poll_stop.set()
            try:
                poller.join(timeout=1.0)
            except Exception:
                pass

        if not self._gen_ok(gen):
            # Disabled / superseded — do not enter linger.
            return

        # Always reconcile against the messages API. Partial [think] buffers
        # used to skip this when len>40 and hide the real assistant reply.
        rows = fetch_run_messages(creds, run_id)
        with self._lock:
            q = self.state.query
        if not q:
            q = extract_user_query(rows)
        api_text = ordered_unique_message_text(rows)
        text_now = "\n".join(buf_lines).strip()
        with self._lock:
            prior = (self.state.text or "").strip()
        for candidate in (text_now, prior, api_text):
            if not candidate or _is_placeholder(candidate):
                continue
            stripped = candidate.replace("\n\n— turn complete —", "").strip()
            text_now = merge_turn_bodies(text_now, stripped)

        status = fetch_run_status(creds, run_id)
        still = run_still_in_flight(status) and time.time() < wall_deadline
        # If the run is still going and we have no terminal event, keep the
        # overlay up and poll messages until complete or wall clock.
        if still and self._gen_ok(gen):
            wait_end = min(wall_deadline, time.time() + 120.0)
            while (
                time.time() < wait_end
                and not self._stop_event.is_set()
                and self._gen_ok(gen)
            ):
                status = fetch_run_status(creds, run_id)
                rows = fetch_run_messages(creds, run_id)
                api_text = ordered_unique_message_text(rows)
                if api_text:
                    _apply_text(api_text)
                if not run_still_in_flight(status):
                    break
                with self._lock:
                    if gen == self._generation and self.state.enabled:
                        self.state.status = "streaming"
                        if _is_placeholder(self.state.text):
                            self.state.text = waiting_panel_text(
                                agent_name=creds.agent_name,
                                run_id=run_id,
                                query=self.state.query,
                            )
                self._notify()
                time.sleep(0.8)
            rows = fetch_run_messages(creds, run_id)
            api_text = ordered_unique_message_text(rows)
            status = fetch_run_status(creds, run_id)

        follow = None
        with self._lock:
            if self._followup_agent == creds.agent_name:
                follow = self._followup_agent
                self._followup_agent = None
        if follow and self._gen_ok(gen):
            # Only honor an armed follow-up when Broca still has a *new*
            # active turn (different queue id / different query). Same-turn
            # LIVE hardcopy noise must not clear the linger panel.
            turn = fetch_broca_current_turn(self.agents_root, creds.agent_name)
            new_q = clean_user_query(str(turn.get("message") or ""))
            with self._lock:
                old_q = normalized_query(self.state.query)
            new_qid = turn.get("queue_id")
            armed_qid = None
            with self._lock:
                armed_qid = getattr(self, "_armed_queue_id", None)
            same = (
                not turn.get("active")
                or (
                    new_qid is not None
                    and armed_qid is not None
                    and new_qid == armed_qid
                )
                or (
                    new_q
                    and old_q
                    and normalized_query(new_q) == old_q
                )
            )
            if not same and new_q:
                with self._lock:
                    if gen == self._generation and self.state.enabled:
                        self.state.active = False
                        self.state.lingering = False
                        self.state.status = "idle"
                        self.state.text = ""
                        self.state.query = ""
                        self.state.saw_waiting_query = False
                        self.state.error = ""
                        self.state.run_id = ""
                self._start_seek(
                    creds,
                    since=time.time(),
                    initial_query=new_q,
                    recovery_query=new_q,
                )
                return
            trace(
                "turn",
                "followup_skip",
                agent=creds.agent_name,
                reason="same_turn_or_idle",
                broca_active=bool(turn.get("active")),
            )

        # A completed run's messages endpoint is authoritative. The previous
        # length-based merge could retain a longer reasoning trace and discard
        # a shorter payload that contained the final assistant message.
        if api_text and not run_still_in_flight(status):
            text_now = api_text
        elif api_text:
            text_now = merge_turn_bodies(
                text_now.replace("\n\n— turn complete —", "").strip(),
                api_text,
            )

        with self._lock:
            if gen != self._generation or not self.state.enabled:
                return
            q = self.state.query or q
            if text_now:
                if api_text and not run_still_in_flight(status):
                    self.state.text = bounded_turn_text(api_text, q)
                else:
                    merged = merge_turn_bodies(
                        self.state.text.replace("\n\n— turn complete —", "").strip(),
                        with_query_header(text_now, q),
                    )
                    self.state.text = bounded_turn_text(merged, q)
            elif _is_placeholder(self.state.text or ""):
                self.state.text = (
                    f"No stream content for {run_id}."
                    + (f" ({stream_error})" if stream_error else "")
                )
            final = (self.state.text or "").strip()
            thin = len(final) < 40
            self.state.status = "error" if (stream_error and thin) else "linger"
            self.state.lingering = True
            self.state.active = True
            self.state.linger_until = time.time() + self.LINGER_S
            if final and not final.endswith("— turn complete —"):
                self.state.text = final + "\n\n— turn complete —"
            trace(
                "turn",
                "stream_end",
                agent=creds.agent_name,
                run=(run_id or "")[-12:],
                status=status,
                chars=len(api_text or ""),
                error=(stream_error or "")[:120],
            )
        self._notify()

    def _notify(self) -> None:
        if self._shutting_down or not self.on_update:
            return
        try:
            self.on_update()
        except Exception as exc:  # noqa: BLE001 — UI may already be gone
            trace("turn", "notify_error", error=type(exc).__name__, detail=str(exc)[:120])
