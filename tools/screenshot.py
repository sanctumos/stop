"""Capture Textual screenshots at three widths; optionally convert SVG→PNG."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from stop.app import StopApp
from stop.host import FixtureHost, LiveHost


def _svg_to_png(svg_path: Path, png_path: Path) -> None:
    try:
        import cairosvg
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(f"cairosvg required for PNG: {exc}") from exc
    cairosvg.svg2png(url=str(svg_path), write_to=str(png_path))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--fixture", help="fixture dir (omit for live host)")
    p.add_argument("--live", action="store_true", help="snapshot live moya host")
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--png", action="store_true", help="also write PNG via cairosvg")
    args = p.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.live:
        host = LiveHost()
        tag = "live"
    elif args.fixture:
        host = FixtureHost(Path(args.fixture))
        tag = "fixture"
    else:
        raise SystemExit("need --fixture or --live")

    sizes = [(160, 45, "wide"), (100, 30, "medium"), (60, 40, "narrow")]

    async def run_all() -> None:
        for cols, rows, label in sizes:
            app = StopApp(host)
            async with app.run_test(size=(cols, rows)) as pilot:
                app.refresh_host()
                await pilot.pause(0.15)
                # Exercise help once on wide so we know overlay renders.
                if label == "wide":
                    await pilot.press("question_mark")
                    await pilot.pause(0.1)
                    help_svg = app.export_screenshot()
                    help_dest = out / f"stop-{tag}-help-{cols}x{rows}.svg"
                    help_dest.write_text(help_svg)
                    print(f"wrote {help_dest}")
                    if args.png:
                        _svg_to_png(help_dest, help_dest.with_suffix(".png"))
                        print(f"wrote {help_dest.with_suffix('.png')}")
                    await pilot.press("escape")
                    await pilot.pause(0.05)
                svg = app.export_screenshot()
                dest = out / f"stop-{tag}-{label}-{cols}x{rows}.svg"
                dest.write_text(svg)
                print(f"wrote {dest}")
                if args.png:
                    png = dest.with_suffix(".png")
                    _svg_to_png(dest, png)
                    print(f"wrote {png}")

    asyncio.run(run_all())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
