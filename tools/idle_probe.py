"""Multi-tick idle probe for stop TUI data stability (run on moya)."""

from __future__ import annotations

import hashlib
import time

from stop.activity import pick_active_now
from stop.host import LiveHost
from stop.livelog import LiveLogFeed, lines_from_scrollback


class FakeLog:
    def __init__(self) -> None:
        self.ops: list[str] = []

    def clear(self) -> None:
        self.ops.append("CLEAR")

    def write(self, line: str, scroll_end: bool | None = None) -> None:
        self.ops.append("W")


def main() -> None:
    h = LiveHost()
    h.hardcopy_interval_s = 0.5
    feed_a = LiveLogFeed(limit=400, width=200)
    feed_act = LiveLogFeed(limit=200, width=200)
    log_a = FakeLog()
    log_act = FakeLog()

    snap = h.snapshot()
    athena = next(a for a in snap.agents if a.name == "athena")
    w0 = next(x for x in athena.windows if (x.screen_name or "").startswith("broca-"))
    h.set_focus_screens({w0.screen_name})

    prev: dict = {}
    for i in range(10):
        time.sleep(1.05)
        log_a.ops.clear()
        log_act.ops.clear()
        snap = h.snapshot()
        athena = next(a for a in snap.agents if a.name == "athena")
        w = next(x for x in athena.windows if (x.screen_name or "").startswith("broca-"))
        text = w.last_scrollback or ""
        md5 = hashlib.md5(text.encode()).hexdigest()[:12]
        lines = lines_from_scrollback(text, limit=400)
        mode = feed_a.sync(log_a, text, source_key=f"athena:{w.id}")
        active = pick_active_now(snap.agents)
        amode = "none"
        if active:
            amode = feed_act.sync(
                log_act, active.last_scrollback or "", source_key=active.id
            )
        cur = {
            "md5": md5,
            "bridge": (w.bridge_inbox_count, w.bridge_outbox_count),
            "cpu": int(snap.cpu_percent),
            "active": active.id if active else None,
            "nlines": len(lines),
            "tail": lines[-1] if lines else "",
        }
        changed = [k for k, v in cur.items() if prev.get(k) != v]
        prev = cur
        print(
            f"t{i} athena_sync={mode} active_sync={amode} "
            f"ops_a={len(log_a.ops)} ops_act={len(log_act.ops)} "
            f"changed={changed or ['stable']} "
            f"bridge={cur['bridge']} active={active.label if active else None}"
        )


if __name__ == "__main__":
    main()
