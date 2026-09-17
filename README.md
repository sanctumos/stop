# stop — SanctumOS-top

A btop-shaped, **read-only** process-manager TUI for a Sanctum host. Instead of OS
PIDs it lists **agents** (the `broca-<agent>` GNU screen sessions plus `letta` and
`smcp`), lets you arrow-key into an agent to see its windows, and keeps an
always-on **Active Now** pane pointed at whatever non-Letta surface moved last.

Design + plan: DSC Tasks Doc #1376 (SanctumOS board, list "stop (SanctumOS procman TUI)").

## Status

v0 scaffold. Nothing to see yet.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -e .[dev]
.venv/bin/stop                 # live host (moya)
.venv/bin/stop --fixture tests/fixtures/basic   # fake host for dev
```

## Guarantees

- Never writes into `~/sanctum/**`, never touches any agent DB, never sends screen
  commands other than `hardcopy`. Scratch lives in `/tmp/stop-<uid>/`.
- No restart buttons. Crashed windows go red and `stop` waits for the cron
  supervisor to bring them back.

## Keys

`↑↓`/`jk` select agent · `Enter`/`Esc` expand pane · `Tab` cycle panes · `a` Active Now ·
`f` follow-lock · `/` filter · `?` help · `q` quit. `Ctrl+S` is never bound.
