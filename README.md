# stop — SanctumOS-top

A btop-shaped, **read-only** process-manager TUI for a Sanctum host. Instead of OS
PIDs it lists **agents** (the `broca-<agent>` GNU screen sessions plus system
screens such as `letta` / `smcp`), lets you arrow-key into an agent to see its
windows, and keeps an always-on **Active Now** pane pointed at whatever non-Letta
surface moved last.

Design + plan: DSC Tasks Doc #1376 (SanctumOS board, list "stop (SanctumOS procman TUI)").

## Status

v0.1 — customer-hardening pass. Works on moya today; layout is config/CLI driven
so a non-`~/sanctum` tree can run without rewriting code.

## Privilege warning — shared screen

`screen -x stop` (or whatever `STOP_SCREEN_NAME` you chose) is a **privileged
console**, not a public dashboard. Anyone who can attach sees agent scrollback
and turn-popup query/stream text. Treat attach rights like SSH to the box.

## Shared screen — do not destroy it

**Never** kill, quit, or recreate the shared watch session from automation — it
forces every attached operator to rejoin.

To pick up a new git revision on an existing install:

```bash
git -C "$STOP_REPO" pull --ff-only
"$STOP_REPO/tools/reload-in-screen.sh"   # relaunch TUI inside screen; session stays
```

If someone is **Attached**, that script **skips** the relaunch (restarting `stop`
resets selection/scroll). Detach first, or `STOP_RELOAD_FORCE=1` only when
explicitly requested. Start the TUI **without** `exec` so `q` leaves bash alive
inside the screen.

## Install (customer / fresh machine)

```bash
git clone <this-repo> stop && cd stop
python3 -m venv .venv
.venv/bin/pip install -c constraints.txt -e '.[dev]'
export STOP_AGENTS_ROOT=/path/to/agents   # optional; default ~/sanctum/agents
export STOP_LOGS_ROOT=/path/to/logs       # optional; default ~/logs
.venv/bin/stop
```

Optional `~/.config/stop/config.toml`:

```toml
agents_root = "/opt/sanctum/agents"
logs_root = "/var/log/sanctum"
refresh_host_s = 1.0
# Extra Active Now classifiers for a foreign Broca dialect:
# noise_patterns = ["Retrieved 0\\b.*\\bmessages\\b"]
# dialogue_patterns = ["\\binbound\\b"]
```

CLI overrides: `--agents-root`, `--logs-root`, `--config`, `--fixture`.

Cron launcher (no hardcoded home user): set `STOP_REPO` or rely on the script’s
own directory; lock file defaults to `/tmp/stop-<uid>-screen-start.lock`.

```bash
# Example — replace paths
@reboot /opt/stop/tools/start-in-screen.sh >>"$HOME/logs/stop-cron.log" 2>&1
* * * * * /opt/stop/tools/start-in-screen.sh >>"$HOME/logs/stop-cron.log" 2>&1
```

## Run (dev)

```bash
.venv/bin/pip install -c constraints.txt -e '.[dev]'
.venv/bin/stop                              # live host
.venv/bin/stop --fixture tests/fixtures/basic
```

## Guarantees

- Never writes into the agents tree; never sends screen commands other than
  `hardcopy`. Scratch lives in `/tmp/stop-<uid>/` (bounded metrics, errors, trace).
- Architecture: three lanes (collection / state / render) — see
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Widgets must not do host I/O.
- Turn ask text uses published Otto bridge HTTP ``GET /v1/turn/current``
  (never opens Broca ``sanctum.db`` from stop).
- Outbound Letta/Broca HTTP: loopback or https; redirects drop `Authorization`.
- No restart buttons. Crashed windows go red and `stop` waits for the cron
  supervisor to bring them back.
- Never destroy the shared `stop` screen (see above).

## Keys

| Key | Action |
|-----|--------|
| `↑↓` / `j` `k` | Select agent |
| `Enter` / `Esc` | Expand / collapse window pane |
| `Tab` | Cycle panes (narrow: agents → windows → Active Now → Letta) |
| `a` / `l` | Jump Active Now / Letta |
| `t` | Turn popup for the **selected** agent only (default ON) |
| `f` | **Follow** — turn popup for **any** agent; a newer turn drops the current one (turns `t` off) |
| `p` | Pin / release Active Now on the current hottest window |
| `PgUp` / `PgDn` | Scroll focused log (holds follow-tail) |
| `Home` / `End` | Log start / resume follow-tail at end |
| `/` | Filter agents |
| `?` | Help |
| `q` | Quit |

Chrome strip shows layout/page name, active turn, and `scroll:held` when follow-tail is off.
`Ctrl+S` is never bound.
