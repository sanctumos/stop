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
    # Pretend we are the UI thread so counter records the current defect.
    host.ui_thread_ident = __import__("threading").get_ident()
    host.set_focus_screens({"broca-athena", "letta"})
    latencies: list[float] = []
    feed = LiveLogFeed(limit=400)
    log = FakeLog()
    for i in range(8):
        t0 = time.perf_counter()
        snap = host.snapshot()
        latencies.append(time.perf_counter() - t0)
        athena = next((a for a in snap.agents if a.name == "athena"), None)
        if athena:
            w = next(
                (x for x in athena.windows if (x.screen_name or "").startswith("broca-")),
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
            "host_io_on_ui_thread > 0 documents defect #4061",
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


if __name__ == "__main__":
    raise SystemExit(main())
