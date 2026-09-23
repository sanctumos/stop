"""Customer-hardening coverage (#4445–#4458)."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from stop.host import sanitize_screen_name
from stop.http_safe import HttpPolicyError, build_opener, validate_outbound_url
from stop.metrics import StopMetrics, metrics_dir
from stop.scratch import append_bounded
from stop.telemetry import TraceRing, _redact
from stop.turn_stream import TurnStreamWorker


def test_sanitize_screen_name_rejects_path_escape():
    assert sanitize_screen_name("broca-athena") == "broca-athena"
    assert sanitize_screen_name("../etc/passwd") is None
    assert sanitize_screen_name("evil/name") is None
    assert sanitize_screen_name("a" * 100) is None
    assert sanitize_screen_name("") is None


def test_validate_outbound_blocks_non_loopback_http():
    validate_outbound_url("http://127.0.0.1:8284")
    validate_outbound_url("https://example.com/v1")
    with pytest.raises(HttpPolicyError):
        validate_outbound_url("http://evil.example/v1")


def test_redirect_strips_authorization(monkeypatch):
    """Redirect handler must not re-send Bearer to the next hop."""
    seen: list[dict] = []

    class FakeResp:
        def __init__(self, *, code=200, headers=None, body=b"{}"):
            self.status = code
            self.code = code
            self.headers = headers or {}
            self._body = body
            self.msg = "OK"
            self.reason = "OK"
            self.url = "http://127.0.0.1/final"

        def read(self):
            return self._body

        def geturl(self):
            return self.url

        def info(self):
            return self.headers

        def getcode(self):
            return self.code

        def close(self):
            return None

    def fake_http_open(self, req, **kw):  # noqa: ANN001
        seen.append(dict(req.headers))
        if len(seen) == 1:
            # 302 to another loopback URL
            resp = FakeResp(
                code=302,
                headers={"Location": "http://127.0.0.1:9/next"},
                body=b"",
            )
            return resp
        return FakeResp(body=b'{"ok":true}')

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", fake_http_open)
    opener = build_opener()
    req = urllib.request.Request(
        "http://127.0.0.1:8/start",
        headers={"Authorization": "Bearer super-secret-token-value"},
    )
    # First hop may raise HTTPError on 302 depending on handler chain —
    # exercise redirect_request directly.
    handler = [h for h in opener.handlers if hasattr(h, "redirect_request")][0]
    class FP:
        pass

    new = handler.redirect_request(
        req,
        FP(),
        302,
        "Found",
        {"Location": "http://127.0.0.1:9/next"},
        "http://127.0.0.1:9/next",
    )
    assert new is not None
    assert "Authorization" not in new.headers
    assert "authorization" not in {k.lower() for k in new.headers}


def test_trace_scrubs_bearer_in_free_text(tmp_path: Path):
    ring = TraceRing(path=tmp_path / "trace.json", load=False)
    ring.event(
        "turn",
        "stream_error",
        detail="failed Authorization: Bearer abcdefghijklmnop123456",
    )
    raw = (tmp_path / "trace.json").read_text(encoding="utf-8")
    assert "abcdefghijklmnop123456" not in raw
    assert "[redacted]" in raw


def test_redact_drops_secret_keys():
    out = _redact({"api_key": "secret", "ok": "fine", "error": "token sk-abcdefghijklmnopqrst"})
    assert "api_key" not in out
    assert out["ok"] == "fine"
    assert "[redacted]" in out["error"]


def test_shutdown_clears_callback_and_joins(tmp_path: Path):
    calls = {"n": 0}

    def on_update():
        calls["n"] += 1

    w = TurnStreamWorker(agents_root=tmp_path, on_update=on_update)
    started = threading.Event()
    release = threading.Event()

    def blocker(*_a, **_k):
        started.set()
        release.wait(3.0)

    broca = tmp_path / "athena" / "broca"
    broca.mkdir(parents=True)
    (broca / ".env").write_text(
        "AGENT_ID=agent-x\nAGENT_API_KEY=k\nAGENT_ENDPOINT=http://127.0.0.1:9\n",
        encoding="utf-8",
    )
    with patch.object(w, "_seek_and_stream", blocker):
        from datetime import datetime

        now = time.time()
        ts = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
        live = f"[{ts}] INFO Processing message in LIVE mode\n"
        w.tick(selected_agent="athena", broca_scrollback="idle\n", now=now)
        w.tick(selected_agent="athena", broca_scrollback="idle\n" + live, now=now)
        assert started.wait(2.0)
        w.shutdown(timeout=2.0)
        release.set()
    assert w.on_update is None
    assert w.snapshot().status == "off"
    t = w._thread
    if t is not None:
        assert not t.is_alive()


def test_metrics_jsonl_stays_bounded(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("stop.metrics.metrics_dir", lambda uid=None: tmp_path)
    m = StopMetrics()
    out = tmp_path / "metrics.jsonl"
    for _ in range(80):
        m.snapshot_count += 1
        m.flush(path=out)
    assert out.stat().st_size <= 256 * 1024 + 512


def test_append_bounded_trims(tmp_path: Path):
    p = tmp_path / "err.log"
    append_bounded(p, ("x" * 100 + "\n") * 50, max_bytes=800)
    assert p.stat().st_size <= 900


def test_configure_classifiers_noise():
    from stop import activity

    activity.configure_classifiers(noise_patterns=[r"CUSTOM_NOISE_XYZ"])
    assert activity.classify_line("hello CUSTOM_NOISE_XYZ there") == activity.KIND_NOISE
    activity.configure_classifiers()  # reset


def test_no_letta_server_password_fallback(tmp_path: Path):
    from stop.turn_stream import load_agent_creds

    d = tmp_path / "athena" / "broca"
    d.mkdir(parents=True)
    (d / ".env").write_text(
        "AGENT_ID=agent-x\nLETTA_SERVER_PASSWORD=host-wide\n"
        "AGENT_ENDPOINT=http://127.0.0.1:8284\n",
        encoding="utf-8",
    )
    assert load_agent_creds(tmp_path, "athena") is None
    (d / ".env").write_text(
        "AGENT_ID=agent-x\nAGENT_API_KEY=per-agent\n"
        "AGENT_ENDPOINT=http://127.0.0.1:8284\n",
        encoding="utf-8",
    )
    creds = load_agent_creds(tmp_path, "athena")
    assert creds is not None
    assert creds.api_key == "per-agent"
