#!/usr/bin/env python3
"""Record a stop architecture baseline (run on moya for live numbers).

Writes JSON to stdout and optionally /tmp/stop-<uid>/baseline.json.
No secrets.
"""

from __future__ import annotations

import json
import os
import resource
import time
from pathlib import Path

from stop.host import LiveHost
from stop.livelog import LiveLogFeed
from stop.metrics import METRICS, metrics_dir


class FakeLog:
    def __init__(self) -> None:
        self.ops: list[str] = []

    def clear(self) -> None:
        self.ops.append("CLEAR")

    def write(self, line: str, scroll_end: bool | None = None) -> None:
        self.ops.append("W")


def main() -> int:
    host = LiveHost()
    from stop.collector import HostCollector

    col = HostCollector(host)
    # UI would set this; collector clears it before snapshot.
    host.ui_thread_ident = __import__("threading").get_ident()
    col.start()
    try:
        host.set_focus_screens(["broca-athena", "letta"])
        latencies: list[float] = []
        feed = LiveLogFeed(limit=400)
        log = FakeLog()
        snap = None
        for i in range(8):
            t0 = time.perf_counter()
            col.request()
            deadline = time.time() + 2.0
            prev_rev = col.latest()[0]
            while time.time() < deadline:
                rev, snap, _ = col.latest()
                if rev > prev_rev and snap is not None:
                    break
                time.sleep(0.01)
            latencies.append(time.perf_counter() - t0)
            if snap:
                athena = next((a for a in snap.agents if a.name == "athena"), None)
                if athena:
                    w = next(
                        (
                            x
                            for x in athena.windows
                            if (x.screen_name or "").startswith("broca-")
                        ),
                        None,
                    )
                    if w and i == 0:
                        feed.sync(log, w.last_scrollback or "", source_key=w.id)
                        log.ops.clear()
                    elif w:
                        feed.sync(log, w.last_scrollback or "", source_key=w.id)
            time.sleep(0.35)
        ru = resource.getrusage(resource.RUSAGE_SELF)
        out = {
            "host": "live",
            "uid": os.getuid(),
            "latency_s": {
                "min": round(min(latencies), 4),
                "max": round(max(latencies), 4),
                "mean": round(sum(latencies) / len(latencies), 4),
                "samples": [round(x, 4) for x in latencies],
            },
            "rss_kib": getattr(ru, "ru_maxrss", None),
            "agents": len(snap.agents) if snap else 0,
            "idle_log_ops_after_first_paint": len(log.ops),
            "metrics": METRICS.snapshot_dict(),
            "notes": [
                "collector path: host_io_on_ui_thread should stay 0",
                "turn overlay geometry defect tracked by #4064",
            ],
        }
        text = json.dumps(out, indent=2)
        print(text)
        d = metrics_dir()
        d.mkdir(mode=0o700, exist_ok=True)
        (d / "baseline.json").write_text(text + "\n", encoding="utf-8")
        METRICS.flush()
        print(f"# wrote {d / 'baseline.json'}", flush=True)
        return 0
    finally:
        col.stop()


if __name__ == "__main__":
    raise SystemExit(main())
