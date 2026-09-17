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
    p.add_argument("--version", action="version", version=f"stop {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # TUI lands in slice #4044. For now prove the entry point works.
    mode = f"fixture:{args.fixture}" if args.fixture else "live"
    print(f"stop {__version__} — scaffold only ({mode}). TUI not built yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
