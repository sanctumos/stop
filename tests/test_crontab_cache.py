"""crontab -l must not run on every 1s host tick (syslog flood)."""

from __future__ import annotations

import os
import time

from stop.host import LiveHost


def test_read_crontab_cached_within_interval(tmp_path, monkeypatch):
    host = LiveHost(home=tmp_path)
    host.tmp = tmp_path / "stop-tmp"
    host.tmp.mkdir()
    host._crontab_cache_interval_s = 300.0
    calls = {"n": 0}
    clock = {"t": 1000.0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        assert cmd[:2] == ["crontab", "-l"]
        return type("R", (), {"returncode": 0, "stdout": "* * * * * true\n", "stderr": ""})()

    monkeypatch.setattr("stop.host.subprocess.run", fake_run)
    monkeypatch.setattr("stop.host.time.monotonic", lambda: clock["t"])

    first = host._read_crontab()
    second = host._read_crontab()
    clock["t"] += 30.0
    third = host._read_crontab()
    assert first == second == third == "* * * * * true\n"
    assert calls["n"] == 1

    # Memory expired; on-disk crontab.txt is still fresh — no LIST.
    host._crontab_cache = ""
    host._crontab_cache_epoch = 0.0
    clock["t"] += 1.0
    fourth = host._read_crontab()
    assert fourth == first
    assert calls["n"] == 1

    # Expire the disk copy — LIST once, then rewrite crontab.txt.
    cache = host.tmp / "crontab.txt"
    old = time.time() - 400
    os.utime(cache, (old, old))
    host._crontab_cache = ""
    host._crontab_cache_epoch = 0.0
    fifth = host._read_crontab()
    assert fifth == first
    assert calls["n"] == 2


def test_read_crontab_uses_fresh_disk_cache_without_list(tmp_path, monkeypatch):
    host = LiveHost(home=tmp_path)
    host.tmp = tmp_path / "stop-tmp"
    host.tmp.mkdir()
    host._crontab_cache_interval_s = 300.0
    (host.tmp / "crontab.txt").write_text("* * * * * cached\n", encoding="utf-8")
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        return type("R", (), {"returncode": 0, "stdout": "should-not-run\n", "stderr": ""})()

    monkeypatch.setattr("stop.host.subprocess.run", fake_run)
    text = host._read_crontab()
    assert text == "* * * * * cached\n"
    assert calls["n"] == 0
