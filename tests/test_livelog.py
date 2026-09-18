"""Append-only live log sync."""

from stop.livelog import LiveLogFeed, diff_log_lines, lines_from_scrollback


def test_clean_log_line_strips_rich_level_diamonds():
    from stop.livelog import clean_log_line

    raw = "[2026-09-17 16:44:49] [5🔵 INFO] httpx: HTTP Request"
    out = clean_log_line(raw)
    assert "🔵" not in out
    assert "◆" not in out
    assert "[5| INFO]" in out or "[5|INFO]" in out.replace(" ", "")
    mangled = "[2026-09-17 16:44:49] [5\ufffd INFO] plug"
    assert "|" in clean_log_line(mangled)


def test_diff_pure_append():
    assert diff_log_lines(["a"], ["a", "b", "c"]) == ("append", ["b", "c"])


def test_diff_sliding_tail_overlap():
    old = ["1", "2", "3", "4"]
    new = ["3", "4", "5"]  # hardcopy window slid
    assert diff_log_lines(old, new) == ("append", ["5"])


def test_diff_replace_on_jump():
    assert diff_log_lines(["a", "b"], ["x", "y"]) == ("replace", ["x", "y"])


def test_diff_rejects_truncated_hardcopy():
    old = [f"line-{i}" for i in range(80)]
    assert diff_log_lines(old, []) == ("noop", [])
    assert diff_log_lines(old, old[:5]) == ("noop", [])


def test_feed_idle_is_noop():
    writes: list[str] = []

    class FakeLog:
        def clear(self) -> None:
            writes.append("CLEAR")

        def write(self, line: str, scroll_end: bool | None = None) -> None:
            writes.append(line)

    feed = LiveLogFeed(limit=50)
    log = FakeLog()
    assert feed.sync(log, "one\ntwo\n", source_key="a") == "replace"
    assert feed.sync(log, "one\ntwo\n", source_key="a") == "noop"
    assert feed.sync(log, "one\ntwo\nthree\n", source_key="a") == "append"
    assert writes == ["CLEAR", "one", "two", "three"]


def test_feed_ignores_empty_after_good_buffer():
    writes: list[str] = []

    class FakeLog:
        def clear(self) -> None:
            writes.append("CLEAR")

        def write(self, line: str, scroll_end: bool | None = None) -> None:
            writes.append(line)

    feed = LiveLogFeed(limit=50)
    log = FakeLog()
    feed.sync(log, "one\ntwo\nthree\n", source_key="a")
    assert feed.sync(log, "", source_key="a") == "noop"
    assert writes.count("CLEAR") == 1
