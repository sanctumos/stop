# stop — SanctumOS-top

A btop-shaped, **read-only** process-manager TUI for a Sanctum host. Instead of OS
PIDs it lists **agents** (the `broca-<agent>` GNU screen sessions plus `letta` and
`smcp`), lets you arrow-key into an agent to see its windows, and keeps an
always-on **Active Now** pane pointed at whatever non-Letta surface moved last.

Design + plan: DSC Tasks Doc #1376 (SanctumOS board, list "stop (SanctumOS procman TUI)").

## Status

v1 on moya. Shared watch screen: `screen -x stop` (as `rizzn`).

## Shared screen — do not destroy it

Mark attaches with `screen -x stop`. **Never** kill, quit, or recreate that
session from automation — it forces a rejoin.

To pick up a new git revision on moya:

```bash
git -C ~/sanctum/repos/stop pull --ff-only
~/sanctum/repos/stop/tools/reload-in-screen.sh   # relaunch TUI inside screen; session stays
```

If Mark is **Attached**, that script **skips** the relaunch (restarting `stop`
resets selection/scroll). Detach first, or `STOP_RELOAD_FORCE=1` only when he
asks. Start the TUI **without** `exec` so `q` leaves bash alive inside the screen.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -e .[dev]
.venv/bin/stop                 # live host (moya)
.venv/bin/stop --fixture tests/fixtures/basic   # fake host for dev
```

## Guarantees

- Never writes into `~/sanctum/**`, never sends screen commands other than
  `hardcopy`. Scratch lives in `/tmp/stop-<uid>/`.
- Architecture: three lanes (collection / state / render) — see
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Widgets must not do host I/O.
- Turn ask text uses published Otto bridge HTTP ``GET /v1/turn/current``
  (never opens Broca ``sanctum.db`` from stop).
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
