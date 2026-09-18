"""Relative per-agent activity sparklines."""

from stop.models import Agent, Window, WindowState
from stop.pulse import GLYPHS, AgentPulse, activity_text, new_meaningful_lines


def _agent(name: str, text: str, *, log: str = "") -> Agent:
    windows = [
        Window(
            id=f"{name}/broca",
            label=f"broca-{name}",
            screen_name=f"broca-{name}",
            state=WindowState.RUNNING,
            last_scrollback=text,
        )
    ]
    if log:
        windows.append(
            Window(
                id=f"{name}/run-log",
                label="run log",
                log_path=f"/tmp/{name}.log",
                state=WindowState.RUNNING,
                last_scrollback=log,
            )
        )
    return Agent(name=name, windows=windows)


def test_glyphs_are_in_the_moya_console_font():
    # Uni2-Fixed16 on tty1. Eighth-blocks and .:-=+*# are the wrong answer.
    assert GLYPHS == "─░▒█"


def test_blank_screen_uses_that_agents_run_log_only():
    agent = _agent("longfellow", "\n\n", log="Retrieved 0 messages from web chat API\n")
    assert "web chat API" in activity_text(agent)
    assert activity_text(_agent("bramwell", "Bramwell startup\n")) == "Bramwell startup\n"


def test_polling_floor_stays_flat_while_burst_rises():
    pulse = AgentPulse()
    now = 1_000.0
    polling = "[2026-09-18 12:00:00] INFO Retrieved 0 messages from web chat API\n"
    pulse.observe([_agent("porter", polling)], now)
    for step in range(1, 8):
        polling += (
            f"[2026-09-18 12:00:{step:02d}] INFO "
            "Retrieved 0 messages from web chat API\n"
        )
        rendered = pulse.observe([_agent("porter", polling)], now + step)["porter"]
    assert set(rendered) == {GLYPHS[0]}

    quiet = "[2026-09-18 12:00:00] INFO idle\n"
    pulse.observe([_agent("athena", quiet)], now)
    burst = quiet + "[2026-09-18 12:00:05] INFO telegram inbound from Mark\n"
    rendered = pulse.observe([_agent("athena", burst)], now + 5)["athena"]
    assert rendered[-1] != GLYPHS[0]


def test_partner_bridge_empty_poll_stays_flat():
    pulse = AgentPulse()
    now = 3_000.0
    line = (
        "[2026-09-18 14:35:02] INFO "
        "plugins.rico_kitchen_webchat.api_client: Retrieved 0 partner-bridge messages\n"
    )
    pulse.observe([_agent("rico", line)], now)
    text = line
    for step in range(1, 6):
        text += (
            f"[2026-09-18 14:35:{2+step*3:02d}] INFO "
            "plugins.rico_kitchen_webchat.api_client: "
            "Retrieved 0 partner-bridge messages\n"
        )
        rendered = pulse.observe([_agent("rico", text)], now + step)["rico"]
    assert set(rendered) == {GLYPHS[0]}


def test_rolling_hardcopy_counts_only_the_new_line():
    old = "\n".join(f"[2026-09-18 12:00:{i:02d}] INFO telegram inbound {i}" for i in range(10))
    new = "\n".join(f"[2026-09-18 12:00:{i:02d}] INFO telegram inbound {i}" for i in range(1, 11))
    assert new_meaningful_lines(old + "\n", new + "\n") == 1


def test_open_turn_stays_lit_while_letta_is_quiet():
    pulse = AgentPulse()
    now = 5_000.0
    text = (
        "[2026-09-18 15:09:02] INFO Coalesced inbound queued message_id=6911\n"
        "[2026-09-18 15:09:02] INFO Processing message in LIVE mode\n"
    )
    agent = _agent("athena", text)
    assert pulse.observe([agent], now)["athena"][-1] == GLYPHS[-1]
    # Same screen 20s later: she is still inside the Letta call.
    assert pulse.observe([agent], now + 20)["athena"][-1] == GLYPHS[-1]
    closed = text + (
        "[2026-09-18 15:09:40] INFO Routing response through telegram handler\n"
    )
    pulse.observe([_agent("athena", closed)], now + 21)
    faded = pulse.observe([_agent("athena", closed)], now + 70)["athena"]
    assert set(faded) == {GLYPHS[0]}


def test_agents_do_not_share_scale_and_old_bursts_decay():
    pulse = AgentPulse(window_s=30)
    now = 2_000.0
    base = "[2026-09-18 12:00:00] INFO idle\n"
    athena = _agent("athena", base)
    monday = _agent("monday", base)
    pulse.observe([athena, monday], now)
    athena = _agent(
        "athena",
        base + "[2026-09-18 12:00:02] INFO telegram inbound\n",
    )
    hot = pulse.observe([athena, monday], now + 2)
    assert hot["athena"][-1] != GLYPHS[0]
    assert set(hot["monday"]) == {GLYPHS[0]}
    cooled = pulse.observe([athena, monday], now + 40)
    assert set(cooled["athena"]) == {GLYPHS[0]}
