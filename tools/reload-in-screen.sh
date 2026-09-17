#!/usr/bin/env bash
# Reload the stop TUI *inside* the existing shared GNU screen session.
#
# HARD RULE: never kill, quit, or recreate the `stop` screen.
# Mark attaches with `screen -x stop` — destroying that session forces a rejoin.
#
# Usage (on moya as rizzn):
#   ~/sanctum/repos/stop/tools/reload-in-screen.sh
#
# Optional: pull first
#   git -C ~/sanctum/repos/stop pull --ff-only && ~/sanctum/repos/stop/tools/reload-in-screen.sh

set -euo pipefail

SCREEN_NAME="${STOP_SCREEN_NAME:-stop}"

if ! screen -ls 2>&1 | grep -qE "[0-9]+\.${SCREEN_NAME}[[:space:]]"; then
  echo "ERROR: screen session '${SCREEN_NAME}' is not running." >&2
  echo "Do NOT auto-create it from automation while Mark may be attached elsewhere." >&2
  echo "If Mark asks to recreate: screen -dmS ${SCREEN_NAME} -h 20000 bash -l" >&2
  exit 2
fi

# Interrupt the TUI only. Requires stop was started WITHOUT \`exec\` so bash survives.
# Ctrl-C → brief pause → launch stop again. Screen session PID stays the same.
screen -S "$SCREEN_NAME" -X stuff $'\003'
sleep 0.4
screen -S "$SCREEN_NAME" -X stuff $'cd ~/sanctum/repos/stop && source .venv/bin/activate && stop\n'

# Smoke: process should appear within a few seconds.
for _ in 1 2 3 4 5 6 7 8; do
  if pgrep -f "/home/rizzn/sanctum/repos/stop/.venv/bin/stop" >/dev/null 2>&1; then
    echo "ok: stop TUI reloaded inside screen '${SCREEN_NAME}' (session preserved)"
    exit 0
  fi
  sleep 0.5
done

echo "WARN: screen '${SCREEN_NAME}' still exists but stop process not seen yet" >&2
exit 1
