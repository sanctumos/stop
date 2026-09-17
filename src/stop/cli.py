"""Command-line entry point for `stop`."""

from __future__ import annotations

import argparse
import sys

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stop",
        description="SanctumOS-top: read-only procman TUI for Sanctum agents.",
    )
    p.add_argument(
        "--fixture",
        metavar="DIR",
        help="Run against a fake host layout (dev/testing) instead of the live machine.",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Print one discovery snapshot to stdout and exit (no TUI).",
    )
    p.add_argument("--version", action="version", version=f"stop {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.once:
        from pathlib import Path

        from .host import FixtureHost, LiveHost
        from .state import badge_label

        host = FixtureHost(Path(args.fixture)) if args.fixture else LiveHost()
        snap = host.snapshot()
        print(
            f"stop {__version__}  CPU {snap.cpu_percent:.1f}%  "
            f"RAM {snap.mem_used_gib:.1f}/{snap.mem_total_gib:.1f} GiB"
        )
        for agent in snap.agents:
            print(f"  {agent.name:<12} {badge_label(agent.state)}")
            for w in agent.windows:
                print(f"    - {w.label}: {badge_label(w.state)}")
        return 0

    from .app import run_app

    run_app(fixture=args.fixture)
    return 0


if __name__ == "__main__":
    sys.exit(main())
