# stop — architecture (rebuild plan)

Three lanes. Crossing them is how we get freezes, idle “view resets,” and racey overlays.

## Lane 1 — Host collection (I/O)

Owns: `crontab`, `screen -ls`, GNU `screen -X hardcopy`, log tails, bridge directory
counts, `psutil`, and (eventually) any HTTP used only to *discover* host state.

**Rules**

- Runs on a **single serialized background worker** (see Tasks #4061).
- Publishes only **complete, immutable** `HostSnapshot` values with a monotonic
  **revision**.
- Never touches Textual widgets.
- Hardcopy order and budgets are deterministic (Tasks #4062).

## Lane 2 — Application state

Owns: selected agent/window **identity** (not list indexes), follow-lock,
Active Now pick, turn-stream state (generation token, run id, text cursor),
config, and metrics counters.

**Rules**

- Snapshot → view models; compare before notifying renderers (#4063).
- Selection survives inventory churn by **name / window id** (#4067).
- Turn worker writes only when generation is current and enabled (#4066).

## Lane 3 — Textual rendering

Owns: layout, CSS, `RichLog` / `Static` updates, keybindings, screenshots.

**Rules**

- Widgets **must never** run subprocesses, `sleep`, read agent dirs, open DBs,
  or perform network I/O. Collection is requested; rendering only paints.
- Log panes append via `LiveLogFeed`; clear only on true source change or
  empty-state **transition**.
- The turn panel is a **true overlay** on a dedicated layer — it must not
  change `#win-log` geometry or scroll (#4064). Verified defect on current
  `dock: bottom`: at 160×45, `#win-log` height 25 → 14 → 25 on show/hide.

## Metrics

Lightweight counters under `/tmp/stop-<uid>/metrics.jsonl` (no secrets, no
message bodies). See `stop.metrics`.

## Turn popup state machine

Statuses on `TurnStreamState.status` (and `enabled` / `follow_all`):

| From | Event | To | Notes |
|------|-------|-----|-------|
| off | `t` / `f` on | idle | `follow_all` true only for `f` |
| idle | Broca LIVE edge / probe | seeking | selected mode: focused agent only |
| seeking | run found | streaming | SSE + message poller |
| seeking | timeout / no creds | error → linger | short linger |
| streaming | run complete | linger | 60s default |
| linger | timer | idle | cooldown before re-arm |
| any busy | newer start + follow | seeking (other) | preempt closes SSE |
| any | disable / quit | off | closes socket; joins workers |

`pending_agents` is a badge queue in selected mode (background starts while busy).
Drain: cleared on disable; capped display of first three in chrome. Follow mode
preempts instead of queueing.

See Tasks #4448.

## Related Tasks

Parent epic #4041 · rebuild slices #4060–#4074 · customer hardening #4444–#4458.
