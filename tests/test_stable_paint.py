"""Stable paint — idle panes must not flicker on every host tick."""

from __future__ import annotations

import time

from textual.widgets import Static

from stop.app import _age_stable, _paint


class _FakeStatic(Static):
    def __init__(self) -> None:
        super().__init__()
        self.updates: list[str] = []
        self.titles: list[str] = []

    def update(self, content="") -> None:  # type: ignore[override]
        self.updates.append(str(content))

    @property
    def border_title(self):  # type: ignore[override]
        return getattr(self, "_bt", "")

    @border_title.setter
    def border_title(self, value: str) -> None:  # type: ignore[override]
        self._bt = value
        self.titles.append(value)


def test_paint_skips_identical_body():
    w = _FakeStatic()
    _paint(w, "log line\n", title="win")
    _paint(w, "log line\n", title="win")
    _paint(w, "log line\n", title="win · bridge out=2")
    assert w.updates == ["log line\n"]
    assert w.titles == ["win", "win · bridge out=2"]


def test_paint_repaints_when_body_changes():
    w = _FakeStatic()
    _paint(w, "a", title="t")
    _paint(w, "b", title="t")
    assert w.updates == ["a", "b"]


def test_age_stable_does_not_tick_seconds():
    now = time.time()
    assert _age_stable(now - 3, now) == "now"
    assert _age_stable(now - 59, now) == "now"
    assert _age_stable(now - 60, now) == "1m"
