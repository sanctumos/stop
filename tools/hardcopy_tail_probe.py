"""Why does tailed hardcopy flip while the full file is stable?"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from stop.host import LiveHost, SCROLLBACK_MAX_BYTES, SCROLLBACK_MAX_LINES, _tail_text


def main() -> None:
    h = LiveHost()
    h.hardcopy_interval_s = 0.0  # every snapshot
    name = "broca-athena"
    h.set_focus_screens({name})
    out = h.tmp / f"{name}.txt"
    prev_full = prev_tail = prev_lines = None
    for i in range(8):
        time.sleep(1.0)
        # Force hardcopy path
        text = h.read_scrollback(name)
        full = out.read_bytes() if out.is_file() else b""
        full_md5 = hashlib.md5(full).hexdigest()[:10]
        # Manual tail like read_scrollback
        size = len(full)
        if size > SCROLLBACK_MAX_BYTES:
            raw = full[-SCROLLBACK_MAX_BYTES:]
        else:
            raw = full
        decoded = raw.decode("utf-8", errors="replace")
        tailed = _tail_text(decoded)
        lines = tailed.splitlines()
        tail_md5 = hashlib.md5(tailed.encode()).hexdigest()[:10]
        # First/last line fingerprints
        head = lines[0][:60] if lines else ""
        tail = lines[-1][:60] if lines else ""
        ch = []
        if prev_full != full_md5:
            ch.append("full")
        if prev_tail != tail_md5:
            ch.append("tail")
        if prev_lines != len(lines):
            ch.append(f"n={len(lines)}")
        prev_full, prev_tail, prev_lines = full_md5, tail_md5, len(lines)
        print(
            f"t{i} size={size} nlines={len(lines)} changed={ch or ['stable']} "
            f"full={full_md5} tail={tail_md5}"
        )
        print(f"    head={head!r}")
        print(f"    last={tail!r}")
        # Compare to previous line list identity if we have it
        if i > 0 and "tail" in (ch or []):
            # show how many lines match from the end
            pass


if __name__ == "__main__":
    main()
