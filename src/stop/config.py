"""Optional ~/.config/stop/config.toml overrides."""

from __future__ import annotations

import os
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
    # Customer layout — empty means LiveHost defaults (~/sanctum/agents, ~/logs).
    agents_root: str = ""
    logs_root: str = ""
    system_screens: list[str] = field(default_factory=lambda: ["letta", "smcp"])
    noise_patterns: list[str] = field(default_factory=list)
    dialogue_patterns: list[str] = field(default_factory=list)
    pulse_glyphs: str = ""


def load_config(path: Path | None = None) -> StopConfig:
    cfg = StopConfig()
    p = path or (Path.home() / ".config" / "stop" / "config.toml")
    if not p.is_file():
        # Env overrides still apply without a config file.
        _apply_env(cfg)
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
    if "agents_root" in data:
        cfg.agents_root = str(data["agents_root"])
    if "logs_root" in data:
        cfg.logs_root = str(data["logs_root"])
    if "system_screens" in data:
        cfg.system_screens = list(data["system_screens"])
    if "noise_patterns" in data:
        cfg.noise_patterns = list(data["noise_patterns"])
    if "dialogue_patterns" in data:
        cfg.dialogue_patterns = list(data["dialogue_patterns"])
    if "pulse_glyphs" in data:
        cfg.pulse_glyphs = str(data["pulse_glyphs"])
    _apply_env(cfg)
    return cfg


def _apply_env(cfg: StopConfig) -> None:
    if os.environ.get("STOP_AGENTS_ROOT"):
        cfg.agents_root = os.environ["STOP_AGENTS_ROOT"]
    if os.environ.get("STOP_LOGS_ROOT"):
        cfg.logs_root = os.environ["STOP_LOGS_ROOT"]


def apply_agent_order(agents: list, cfg: StopConfig) -> list:
    if cfg.hide_agents:
        hide = set(cfg.hide_agents)
        agents = [a for a in agents if a.name not in hide]
    if not cfg.agent_order:
        return agents
    order = {name: i for i, name in enumerate(cfg.agent_order)}
    return sorted(agents, key=lambda a: (order.get(a.name, 10_000), a.name))
