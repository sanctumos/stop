from pathlib import Path

from stop.config import StopConfig, apply_agent_order, load_config
from stop.models import Agent


def test_load_missing_config(tmp_path: Path):
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.active_exclude == ["letta", "stop"]


def test_load_and_order(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(
        'agent_order = ["rico", "athena"]\n'
        'hide_agents = ["bramwell"]\n'
        'active_exclude = ["letta", "stop", "smcp"]\n'
    )
    cfg = load_config(p)
    assert cfg.hide_agents == ["bramwell"]
    assert "smcp" in cfg.active_exclude
    agents = [
        Agent(name="athena"),
        Agent(name="bramwell"),
        Agent(name="rico"),
        Agent(name="ada"),
    ]
    ordered = apply_agent_order(agents, cfg)
    assert [a.name for a in ordered] == ["rico", "athena", "ada"]
