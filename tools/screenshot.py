"""Capture Textual screenshots at terminal classes; optionally convert SVG→PNG."""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from stop.app import StopApp, WindowPane
from stop.host import FixtureHost, LiveHost
from stop.turn_stream import TurnStreamState


def _svg_to_png(svg_path: Path, png_path: Path) -> None:
    try:
        import cairosvg
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(f"cairosvg required for PNG: {exc}") from exc
    cairosvg.svg2png(url=str(svg_path), write_to=str(png_path))


def _write(app: StopApp, out: Path, name: str, *, png: bool) -> None:
    svg = app.export_screenshot()
    dest = out / f"{name}.svg"
    dest.write_text(svg)
    print(f"wrote {dest}")
    if png:
        _svg_to_png(dest, dest.with_suffix(".png"))
        print(f"wrote {dest.with_suffix('.png')}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--fixture", help="fixture dir (omit for live host)")
    p.add_argument("--live", action="store_true", help="snapshot live moya host")
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--png", action="store_true", help="also write PNG via cairosvg")
    p.add_argument(
        "--qa",
        action="store_true",
        help="capture idle/turn/linger/filter/help/active/letta at all sizes (#4073)",
    )
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

    sizes = [
        (160, 45, "wide"),
        (100, 30, "medium"),
        (60, 40, "narrow"),
        (60, 20, "tiny"),
    ]

    async def run_all() -> None:
        for cols, rows, label in sizes:
            app = StopApp(host)
            async with app.run_test(size=(cols, rows)) as pilot:
                app.refresh_host()
                await pilot.pause(0.2)
                if cols < 80:
                    app._narrow_page = "windows"
                    app._apply_breakpoint()
                    await pilot.pause(0.05)
                app._update_chrome()
                _write(app, out, f"stop-{tag}-idle-{label}-{cols}x{rows}", png=args.png)

                if not args.qa:
                    if label == "wide":
                        await pilot.press("question_mark")
                        await pilot.pause(0.1)
                        _write(app, out, f"stop-{tag}-help-{cols}x{rows}", png=args.png)
                        await pilot.press("escape")
                        await pilot.pause(0.05)
                    continue

                # Active turn overlay
                pane = app.query_one(WindowPane)
                pane.show_turn(
                    TurnStreamState(
                        enabled=True,
                        active=True,
                        agent_name="athena",
                        status="streaming",
                        text="> ask\n\n" + "\n".join(f"chunk {i}" for i in range(20)),
                        started_at=time.time(),
                    )
                )
                app._update_chrome()
                await pilot.pause(0.1)
                _write(app, out, f"stop-{tag}-turn-{label}-{cols}x{rows}", png=args.png)

                pane.show_turn(
                    TurnStreamState(
                        enabled=True,
                        active=True,
                        agent_name="athena",
                        status="linger",
                        lingering=True,
                        text="final linger body\n",
                        started_at=time.time(),
                        linger_until=time.time() + 60,
                    )
                )
                app._update_chrome()
                await pilot.pause(0.1)
                _write(app, out, f"stop-{tag}-linger-{label}-{cols}x{rows}", png=args.png)

                pane.show_turn(TurnStreamState(enabled=True, active=False, status="idle"))
                await pilot.pause(0.05)

                await pilot.press("slash")
                await pilot.pause(0.05)
                _write(app, out, f"stop-{tag}-filter-{label}-{cols}x{rows}", png=args.png)
                await pilot.press("escape")
                await pilot.pause(0.05)

                await pilot.press("question_mark")
                await pilot.pause(0.1)
                _write(app, out, f"stop-{tag}-help-{label}-{cols}x{rows}", png=args.png)
                await pilot.press("escape")
                await pilot.pause(0.05)

                if cols < 80:
                    app._narrow_page = "active"
                    app._apply_breakpoint()
                else:
                    await pilot.press("a")
                await pilot.pause(0.1)
                _write(app, out, f"stop-{tag}-active-{label}-{cols}x{rows}", png=args.png)

                if cols < 80:
                    app._narrow_page = "letta"
                    app._apply_breakpoint()
                else:
                    await pilot.press("l")
                await pilot.pause(0.1)
                _write(app, out, f"stop-{tag}-letta-{label}-{cols}x{rows}", png=args.png)

    asyncio.run(run_all())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
