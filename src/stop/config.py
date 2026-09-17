"""Optional ~/.config/stop/config.toml overrides."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StopConfig:
    agent_order: list[str] = field(default_factory=list)
    hide_agents: list[str] = field(default_factory=list)
    active_exclude: list[str] = field(default_factory=lambda: ["letta", "stop"])
    refresh_host_s: float = 1.0
    refresh_hardcopy_s: float = 2.0


def load_config(path: Path | None = None) -> StopConfig:
    cfg = StopConfig()
    p = path or (Path.home() / ".config" / "stop" / "config.toml")
    if not p.is_file():
        return cfg
    data = tomllib.loads(p.read_text())
    if "agent_order" in data:
        cfg.agent_order = list(data["agent_order"])
    if "hide_agents" in data:
        cfg.hide_agents = list(data["hide_agents"])
    if "active_exclude" in data:
        cfg.active_exclude = list(data["active_exclude"])
    if "refresh_host_s" in data:
        cfg.refresh_host_s = float(data["refresh_host_s"])
    if "refresh_hardcopy_s" in data:
        cfg.refresh_hardcopy_s = float(data["refresh_hardcopy_s"])
    return cfg


def apply_agent_order(agents: list, cfg: StopConfig) -> list:
    if cfg.hide_agents:
        hide = set(cfg.hide_agents)
        agents = [a for a in agents if a.name not in hide]
    if not cfg.agent_order:
        return agents
    order = {name: i for i, name in enumerate(cfg.agent_order)}
    return sorted(agents, key=lambda a: (order.get(a.name, 10_000), a.name))
