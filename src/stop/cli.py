"""Command-line entry point for `stop`."""

from __future__ import annotations

import argparse
import sys
import time

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
        "--config",
        metavar="PATH",
        help="Optional config.toml (default ~/.config/stop/config.toml).",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Print one discovery snapshot to stdout and exit (no TUI).",
    )
    p.add_argument(
        "--watch",
        metavar="SECONDS",
        type=float,
        nargs="?",
        const=1.0,
        help="Text watch mode: print a snapshot every SECONDS (default 1). Ctrl+C to stop.",
    )
    p.add_argument(
        "--watch-log",
        metavar="PATH",
        help="Also append --watch lines to PATH (for smoke proof).",
    )
    p.add_argument(
        "--agents-root",
        metavar="DIR",
        help="Agents tree (default: ~/sanctum/agents or STOP_AGENTS_ROOT / config).",
    )
    p.add_argument(
        "--logs-root",
        metavar="DIR",
        help="Logs tree (default: ~/logs or STOP_LOGS_ROOT / config).",
    )
    p.add_argument("--version", action="version", version=f"stop {__version__}")
    return p


def _print_snapshot(snap, *, version: str = __version__) -> str:
    from .activity import pick_active_now
    from .state import badge_label

    lines = [
        f"stop {version}  CPU {snap.cpu_percent:.1f}%  "
        f"RAM {snap.mem_used_gib:.1f}/{snap.mem_total_gib:.1f} GiB"
    ]
    for agent in snap.agents:
        lines.append(f"  {agent.name:<12} {badge_label(agent.state)}")
        for w in agent.windows:
            lines.append(f"    - {w.label}: {badge_label(w.state)}")
    active = pick_active_now(snap.agents)
    if active:
        preview = (active.last_scrollback or "").strip().splitlines()
        tail = preview[-1][:80] if preview else "(no scrollback)"
        lines.append(f"  Active Now: {active.label} ({badge_label(active.state)}) · {tail}")
    else:
        lines.append("  Active Now: (idle)")
    if snap.events:
        lines.append("  events: " + " · ".join(e.message for e in snap.events[-5:]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.once or args.watch is not None:
        from pathlib import Path

        from .config import load_config
        from .host import FixtureHost, LiveHost

        cfg = load_config(Path(args.config) if args.config else None)
        if getattr(args, "agents_root", None):
            cfg.agents_root = args.agents_root
        if getattr(args, "logs_root", None):
            cfg.logs_root = args.logs_root
        if args.fixture:
            host = FixtureHost(Path(args.fixture))
        else:
            host = LiveHost(
                agents_root=Path(cfg.agents_root) if cfg.agents_root else None,
                logs_root=Path(cfg.logs_root) if cfg.logs_root else None,
            )
        log_fp = open(args.watch_log, "a", encoding="utf-8") if args.watch_log else None
        try:
            if args.once and args.watch is None:
                text = _print_snapshot(host.snapshot())
                print(text)
                if log_fp:
                    log_fp.write(text + "\n")
                return 0

            interval = float(args.watch if args.watch is not None else 1.0)
            print(
                f"stop {__version__} watch every {interval}s "
                f"(Ctrl+C to quit)"
                + (f" · log {args.watch_log}" if args.watch_log else ""),
                flush=True,
            )
            while True:
                ts = time.strftime("%Y-%m-%dT%H:%M:%S")
                block = f"--- {ts} ---\n{_print_snapshot(host.snapshot())}"
                print(block, flush=True)
                if log_fp:
                    log_fp.write(block + "\n")
                    log_fp.flush()
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nwatch stopped", flush=True)
            return 0
        finally:
            if log_fp:
                log_fp.close()

    from .app import run_app

    run_app(
        fixture=args.fixture,
        config_path=args.config,
        agents_root=getattr(args, "agents_root", None),
        logs_root=getattr(args, "logs_root", None),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
