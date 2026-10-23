#!/usr/bin/env bash
# Idempotent stop launcher. Keeps exactly one live `stop` screen.
#
# Same shape as the Broca starters: cron calls this every minute and at boot.
# A live session is left alone (attached or not). A dead socket is wiped.
# The shell stays under the TUI so tools/reload-in-screen.sh can stuff `q`
# and start stop again without destroying the screen.
#
# Customer install: set STOP_REPO to your checkout. Optional STOP_SCREEN_NAME,
# STOP_START_LOCK (defaults to /tmp/stop-<uid>-screen-start.lock).
#
# Example cron (replace the log path as needed):
#   @reboot /path/to/stop/tools/start-in-screen.sh >>"$HOME/logs/stop-cron.log" 2>&1
#   * * * * * /path/to/stop/tools/start-in-screen.sh >>"$HOME/logs/stop-cron.log" 2>&1

set -euo pipefail

SESSION_NAME="${STOP_SCREEN_NAME:-stop}"
if [[ -z "${STOP_REPO:-}" ]]; then
  # Prefer the directory that contains this script (portable checkout).
  STOP_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
LOCK_FILE="${STOP_START_LOCK:-/tmp/stop-$(id -u)-screen-start.lock}"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  exit 0
fi

# Scratch sweeper — runs even when the TUI session already exists.
# Unique hardcopy temps are unlinked in the TUI; leftovers must not pile up.
# crontab -l syslog-logs every LIST, so we refresh a disk cache at most every
# five minutes instead of letting the TUI LIST on every tick.
_STOP_TMP="${STOP_TMP:-/tmp/stop-$(id -u)}"
mkdir -p "$_STOP_TMP"
chmod 700 "$_STOP_TMP" 2>/dev/null || true
find "$_STOP_TMP" -maxdepth 1 -type f -name '*.hc' -mmin +1 -delete 2>/dev/null || true
find "$_STOP_TMP" -maxdepth 1 -type f -name '*.json.tmp' -mmin +1 -delete 2>/dev/null || true
_CT="$_STOP_TMP/crontab.txt"
_NEED_CRONTAB=1
if [[ -f "$_CT" ]]; then
  _CT_AGE=$(( $(date +%s) - $(stat -c %Y "$_CT" 2>/dev/null || echo 0) ))
  if [[ "$_CT_AGE" -ge 0 && "$_CT_AGE" -lt 300 ]]; then
    _NEED_CRONTAB=0
  fi
fi
if [[ "$_NEED_CRONTAB" -eq 1 ]]; then
  if crontab -l >"${_CT}.tmp" 2>/dev/null; then
    chmod 600 "${_CT}.tmp" 2>/dev/null || true
    mv "${_CT}.tmp" "$_CT"
  else
    rm -f "${_CT}.tmp"
  fi
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
