"""Capture Textual screenshots at three widths (fixture mode)."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from stop.app import StopApp
from stop.host import FixtureHost


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--fixture", required=True)
    p.add_argument("--out", required=True, help="output directory for .svg files")
    args = p.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    host = FixtureHost(Path(args.fixture))
    sizes = [(160, 45, "wide"), (100, 30, "medium"), (60, 40, "narrow")]

    async def run_all() -> None:
        for cols, rows, label in sizes:
            app = StopApp(host)
            async with app.run_test(size=(cols, rows)) as pilot:
                app.refresh_host()
                await pilot.pause(0.1)
                svg = app.export_screenshot()
                dest = out / f"stop-{label}-{cols}x{rows}.svg"
                dest.write_text(svg)
                print(f"wrote {dest}")

    asyncio.run(run_all())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
