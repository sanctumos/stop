"""Regression: Broca-style [bracket] log lines must not crash Rich markup."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from stop.app import _plain, _safe_lines


def test_plain_escapes_all_brackets():
    assert _plain("[5 INFO] [/green] path[/x]") == "\\[5 INFO] \\[/green] path\\[/x]"


def test_safe_lines_render_without_markup_error():
    raw = "\n".join(
        [
            "[2026-09-17 17:56:04] [5 INFO] plugins.wren: Retrieved 0 messages",
            "wrote file [/tmp/foo] ok",
            "closing tag [/green] should be literal",
        ]
    )
    lines = _safe_lines(raw, limit=10, width=200)
    assert len(lines) == 3
    buf = StringIO()
    # highlight=False — otherwise Rich's highlighter rewrites [5 INFO] with ANSI
    # and a literal substring assert flakes across Rich versions.
    console = Console(file=buf, force_terminal=True, width=120, highlight=False)
    console.print("\n".join(lines))
    out = buf.getvalue()
    assert "[5 INFO]" in out
    assert "[/green]" in out


def test_safe_lines_strips_controls():
    raw = "hello\x00world\x1b[31mred"
    lines = _safe_lines(raw, limit=5)
    assert "\x00" not in lines[0]
    assert "\x1b" not in lines[0]
