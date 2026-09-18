#!/usr/bin/env bash
# Reload the stop TUI *inside* the existing shared GNU screen session.
#
# HARD RULE: never kill, quit, or recreate the `stop` screen.
# Mark attaches with `screen -x stop` / `screen -r stop` — destroying that
# session forces a rejoin.
#
# HARD RULE: if someone is *attached* to the session, do not restart the TUI
# unless STOP_RELOAD_FORCE=1. Restarting `stop` resets selection, scroll, and
# pane state mid-session even though the screen name survives.
#
# Usage (on moya as rizzn):
#   ~/sanctum/repos/stop/tools/reload-in-screen.sh
#
# Env:
#   STOP_REPO          — repo root (default: dirname of this script / ..)
#   STOP_SCREEN_NAME   — session name (default: stop)
#   STOP_RELOAD_FORCE=1 — allow reload while Attached
#   PATH               — may include a fake `screen` for tests

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STOP_REPO="${STOP_REPO:-$(cd "$SCRIPT_DIR/.." && pwd)}"
SCREEN_NAME="${STOP_SCREEN_NAME:-stop}"
VENV_STOP="$STOP_REPO/.venv/bin/stop"

_screen_ls() {
  screen -ls 2>&1 || true
}

# Count exact matches for N.NAME (reject ambiguous NAME collisions).
_match_lines() {
  _screen_ls | grep -E "[0-9]+\.${SCREEN_NAME}[[:space:]]" || true
}

matches="$(_match_lines)"
count="$(printf '%s\n' "$matches" | grep -c . || true)"
if [[ -z "$matches" || "$count" -eq 0 ]]; then
  echo "ERROR: screen session '${SCREEN_NAME}' is not running." >&2
  echo "Do NOT auto-create it from automation while Mark may be attached elsewhere." >&2
  echo "If Mark asks to recreate: screen -dmS ${SCREEN_NAME} -h 20000 bash -l" >&2
  exit 2
fi
if [[ "$count" -gt 1 ]]; then
  echo "ERROR: ambiguous screen name '${SCREEN_NAME}' ($count sessions)." >&2
  echo "$matches" >&2
  echo "Set STOP_SCREEN_NAME to a unique name or resolve duplicates by hand." >&2
  exit 3
fi

# Resolve exact session id (pid.name) for -S targeting.
SESSION_ID="$(printf '%s\n' "$matches" | head -1 | awk '{print $1}')"
SESSION_ID="${SESSION_ID%%$'\t'*}"
SESSION_ID="${SESSION_ID%% *}"

attached=0
if printf '%s\n' "$matches" | grep -qE "\(Attached\)"; then
  attached=1
fi

if [[ "$attached" -eq 1 && "${STOP_RELOAD_FORCE:-}" != "1" ]]; then
  echo "SKIP: screen '${SCREEN_NAME}' is Attached — not restarting TUI (would reset the live view)." >&2
  echo "Code is on disk; next cold start / Detached reload picks it up." >&2
  echo "To force anyway: STOP_RELOAD_FORCE=1 $0" >&2
  exit 0
fi

# Quit the TUI only (binding `q`). Requires stop was started WITHOUT `exec`
# so bash stays alive inside the screen. Never destroy the GNU screen session
# (no session quit/kill/wipe). Tests assert this script never invokes those.
screen -S "$SESSION_ID" -X stuff 'q'
sleep 0.6
# shellcheck disable=SC2086
screen -S "$SESSION_ID" -X stuff "cd $(printf %q "$STOP_REPO") && source .venv/bin/activate && stop"$'\n'

# Smoke: process should appear within a few seconds.
for _ in 1 2 3 4 5 6 7 8; do
  if [[ -x "$VENV_STOP" ]] && pgrep -f "$VENV_STOP" >/dev/null 2>&1; then
    echo "ok: stop TUI reloaded inside screen '${SESSION_ID}' (session preserved)"
    exit 0
  fi
  # Fallback match: python -m stop / stop entry from this repo.
  if pgrep -f "$STOP_REPO/.venv/bin/python.*stop" >/dev/null 2>&1; then
    echo "ok: stop TUI reloaded inside screen '${SESSION_ID}' (session preserved)"
    exit 0
  fi
  sleep 0.5
done

echo "WARN: screen '${SESSION_ID}' still exists but stop process not seen yet" >&2
exit 1
