"""Append-only live log sync."""

from stop.livelog import LiveLogFeed, diff_log_lines, lines_from_scrollback


def test_diff_noop():
    assert diff_log_lines(["a", "b"], ["a", "b"]) == ("noop", [])


def test_diff_pure_append():
    assert diff_log_lines(["a"], ["a", "b", "c"]) == ("append", ["b", "c"])


def test_diff_sliding_tail_overlap():
    old = ["1", "2", "3", "4"]
    new = ["3", "4", "5"]  # hardcopy window slid
    assert diff_log_lines(old, new) == ("append", ["5"])


def test_diff_replace_on_jump():
    assert diff_log_lines(["a", "b"], ["x", "y"]) == ("replace", ["x", "y"])


def test_feed_idle_is_noop(monkeypatch):
    writes: list[str] = []

    class FakeLog:
        def clear(self) -> None:
            writes.append("CLEAR")

        def write(self, line: str) -> None:
            writes.append(line)

    feed = LiveLogFeed(limit=50)
    log = FakeLog()
    assert feed.sync(log, "one\ntwo\n", source_key="a") == "replace"
    assert feed.sync(log, "one\ntwo\n", source_key="a") == "noop"
    assert feed.sync(log, "one\ntwo\nthree\n", source_key="a") == "append"
    assert writes == ["CLEAR", "one", "two", "three"]
