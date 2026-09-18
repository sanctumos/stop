"""CPU / RAM / swap / network strip.

tty1 uses Uni2-Fixed16 (512 glyphs). Solid block, light and medium shade,
box drawing, and arrows are in that font. Eighth-blocks are not, and the
console draws those as diamonds.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence

from .models import HostSnapshot

# In the console font. Darker means more.
METER = "─░▒█"
BLOCK = "█"
EMPTY = "─"
# A few KB/s of bridge chatter should not paint a solid network graph.
NET_FLOOR_BPS = 4 * 1024


def pressure_style(pct: float) -> str:
    return "green" if pct < 70 else ("yellow" if pct < 90 else "red")


def block_bar(pct: float, width: int) -> str:
    """Filled █ and empty ─. Both are in the tty1 font."""
    width = max(1, width)
    pct = max(0.0, min(100.0, pct))
    filled = int(round((pct / 100.0) * width))
    filled = max(0, min(width, filled))
    return BLOCK * filled + EMPTY * (width - filled)


def shade_graph(values: Sequence[float], width: int, *, floor: float = 0.0) -> str:
    """Newest sample on the right. Scale against this series, not the host."""
    width = max(1, width)
    tail = [max(0.0, v) for v in list(values)[-width:]]
    peak = max(tail) if tail else 0.0
    scale = max(peak, floor)
    cells = [METER[0]] * (width - len(tail))
    for value in tail:
        if scale <= 1e-9 or value <= 0:
            cells.append(METER[0])
            continue
        index = int(round((value / scale) * (len(METER) - 1)))
        index = max(0, min(len(METER) - 1, index))
        cells.append(METER[index])
    return "".join(cells)


def format_rate(bps: float) -> str:
    bps = max(0.0, bps)
    if bps < 1024:
        text = f"{bps:.0f} B/s"
    elif bps < 1024 * 1024:
        text = f"{bps / 1024:.1f} KiB/s"
    else:
        text = f"{bps / (1024 * 1024):.2f} MiB/s"
    return f"{text:>10}"


def _widths(console_width: int) -> tuple[int, int, int, int]:
    if console_width >= 150:
        return 22, 12, 16, 16
    if console_width >= 100:
        return 16, 10, 12, 12
    return 10, 6, 8, 8


class HostMeters:
    """Renderable host strip. Keeps a short network history between ticks."""

    def __init__(self) -> None:
        self.up: deque[float] = deque(maxlen=24)
        self.down: deque[float] = deque(maxlen=24)

    def render(self, snap: HostSnapshot, *, width: int = 80) -> str:
        self.up.append(snap.net_up_bps)
        self.down.append(snap.net_down_bps)
        cpu_w, load_w, mem_w, net_w = _widths(width)

        mem_pct = snap.mem_percent
        if mem_pct <= 0 and snap.mem_total_gib > 0:
            mem_pct = 100.0 * snap.mem_used_gib / snap.mem_total_gib
        cores = max(1, snap.cpu_count)
        load_pct = min(100.0, 100.0 * snap.load_avg[0] / cores)
        one, five, fifteen = snap.load_avg

        cpu = (
            f"[b]CPU[/b] [{pressure_style(snap.cpu_percent)}]"
            f"{block_bar(snap.cpu_percent, cpu_w)}[/] "
            f"{snap.cpu_percent:5.1f}%"
        )
        load = (
            f"[b]LOAD[/b] [{pressure_style(load_pct)}]"
            f"{block_bar(load_pct, load_w)}[/] "
            f"{one:4.2f} {five:4.2f} {fifteen:4.2f}"
        )
        ram = (
            f"[b]RAM[/b] [{pressure_style(mem_pct)}]"
            f"{block_bar(mem_pct, mem_w)}[/] "
            f"{snap.mem_used_gib:4.1f}/{snap.mem_total_gib:.1f}G {mem_pct:3.0f}%"
        )
        if snap.swap_total_gib <= 0:
            swap = "[b]SWAP[/b] [dim]none[/]"
        else:
            swap_pct = snap.swap_percent
            if swap_pct <= 0 and snap.swap_total_gib > 0:
                swap_pct = 100.0 * snap.swap_used_gib / snap.swap_total_gib
            swap = (
                f"[b]SWAP[/b] [{pressure_style(swap_pct)}]"
                f"{block_bar(swap_pct, mem_w)}[/] "
                f"{snap.swap_used_gib:4.1f}/{snap.swap_total_gib:.1f}G {swap_pct:3.0f}%"
            )
        up = shade_graph(self.up, net_w, floor=NET_FLOOR_BPS)
        down = shade_graph(self.down, net_w, floor=NET_FLOOR_BPS)
        net = (
            f"[b]NET[/b] [cyan]↑ {up} {format_rate(snap.net_up_bps)}[/]  "
            f"[magenta]↓ {down} {format_rate(snap.net_down_bps)}[/]"
        )
        return f"{cpu}  {load}\n{ram}  {swap}\n{net}"
