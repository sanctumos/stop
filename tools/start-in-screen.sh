#!/usr/bin/env bash
# Idempotent stop launcher. Keeps exactly one live `stop` screen.
#
# Same shape as the Broca starters on moya: cron calls this every minute
# and at boot. A live session is left alone (attached or not). A dead
# socket is wiped. The shell stays under the TUI so tools/reload-in-screen.sh
# can stuff `q` and start stop again without destroying the screen.
#
# Cron (rizzn on moya):
#   @reboot /home/rizzn/sanctum/repos/stop/tools/start-in-screen.sh >> /home/rizzn/logs/stop-cron.log 2>&1
#   * * * * * /home/rizzn/sanctum/repos/stop/tools/start-in-screen.sh >> /home/rizzn/logs/stop-cron.log 2>&1

set -euo pipefail

SESSION_NAME="${STOP_SCREEN_NAME:-stop}"
STOP_REPO="${STOP_REPO:-/home/rizzn/sanctum/repos/stop}"
LOCK_FILE="${STOP_START_LOCK:-/tmp/stop-screen-start.lock}"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  exit 0
fi

_screen_ls() {
  screen -ls 2>&1 || true
}

_match_lines() {
  _screen_ls | grep -E "[0-9]+\.${SESSION_NAME}[[:space:]]" || true
}

if _screen_ls | grep -q "Dead"; then
  screen -wipe >/dev/null 2>&1 || true
fi

matches="$(_match_lines)"
if [[ -n "$matches" ]]; then
  exit 0
fi

if [[ ! -x "$STOP_REPO/.venv/bin/stop" ]]; then
  echo "ERROR: $STOP_REPO/.venv/bin/stop is missing" >&2
  exit 1
fi

screen -dmS "$SESSION_NAME" -h 20000 bash -l
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if [[ -n "$(_match_lines)" ]]; then
    break
  fi
  sleep 0.2
done
if [[ -z "$(_match_lines)" ]]; then
  echo "ERROR: screen '${SESSION_NAME}' did not appear" >&2
  exit 1
fi

# Let bash -l finish its profile before the launch line is typed.
sleep 0.5
screen -S "$SESSION_NAME" -X stuff "cd $(printf %q "$STOP_REPO") && source .venv/bin/activate && stop"$'\n'
echo "ok: started screen '${SESSION_NAME}'"
