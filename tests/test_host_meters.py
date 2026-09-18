"""Host strip: swap, load, and network, using the tty1 font."""

import re

from stop.host_meters import BLOCK, METER, HostMeters, block_bar
from stop.models import HostSnapshot

_TAGS = re.compile(r"\[/?[^\]]*\]")
_FORBIDDEN = "▁▂▃▄▅▆▇▓"


def _visible(markup: str) -> str:
    return _TAGS.sub("", markup)


def _snap(**kwargs) -> HostSnapshot:
    base = dict(agents=[], screens=[], cpu_count=4)
    base.update(kwargs)
    return HostSnapshot(**base)


def test_zero_is_an_empty_track_and_full_is_a_solid_bar():
    assert block_bar(0, 6) == "──────"
    assert block_bar(100, 6) == "██████"
    assert set(block_bar(40, 10)) <= set(BLOCK + "─")


def test_swap_absent_and_present():
    empty = _visible(HostMeters().render(_snap(), width=80))
    assert "SWAP" in empty
    assert "none" in empty
    used = _visible(
        HostMeters().render(
            _snap(swap_used_gib=1.5, swap_total_gib=8.0, swap_percent=19),
            width=80,
        )
    )
    assert "1.5/8.0G" in used
    assert "none" not in used.split("SWAP", 1)[1]


def test_load_bar_is_share_of_cores_not_the_raw_number():
    text = _visible(HostMeters().render(_snap(load_avg=(2, 1, 0.5)), width=80))
    # 2.0 on 4 cores is half of a 6-cell bar.
    assert "███───" in text
    assert "2.00 1.00 0.50" in text


def test_network_graph_uses_shade_and_ignores_chatter():
    meters = HostMeters()
    quiet = _visible(meters.render(_snap(net_up_bps=80, net_down_bps=100), width=80))
    assert "█" not in quiet
    assert all(ch not in quiet for ch in _FORBIDDEN)
    busy = _visible(meters.render(_snap(net_down_bps=5_000_000), width=80))
    assert "█" in busy
    assert set(ch for ch in busy if ch in METER) <= set(METER)


def test_strip_is_three_lines_and_fits_eighty_columns():
    text = HostMeters().render(
        _snap(
            cpu_percent=95,
            mem_percent=80,
            mem_used_gib=20,
            mem_total_gib=32,
            swap_used_gib=3,
            swap_total_gib=8,
            swap_percent=37,
            load_avg=(3.2, 2.1, 1.4),
            net_up_bps=50_000,
            net_down_bps=2_000_000,
        ),
        width=80,
    )
    lines = _visible(text).splitlines()
    assert len(lines) == 3
    assert all(len(line) <= 78 for line in lines)
    assert all(ch not in text for ch in _FORBIDDEN)
