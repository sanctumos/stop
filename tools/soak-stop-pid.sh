#!/usr/bin/env bash
# Timed stop-process soak — NEVER runs unbounded (#4074 / Mark 2026-09-18).
# Default 120s. Override: SOAK_SECONDS=180 ./tools/soak-stop-pid.sh
set -euo pipefail
SECONDS_MAX="${SOAK_SECONDS:-120}"
PID="${1:-}"
if [[ -z "$PID" ]]; then
  PID="$(pgrep -n -f '/sanctum/repos/stop/.venv/bin/stop' || true)"
fi
if [[ -z "$PID" ]]; then
  echo "no stop pid" >&2
  exit 2
fi
LOG="${SOAK_LOG:-/tmp/stop-soak-timed.log}"
echo "SOAK_START_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ) pid=$PID max=${SECONDS_MAX}s" | tee "$LOG"
end=$((SECONDS + SECONDS_MAX))
n=0
while (( SECONDS < end )); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "DEAD at sample=$n UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "$LOG"
    exit 1
  fi
  n=$((n + 1))
  ps -o etime=,rss=,pcpu=,nlwp= -p "$PID" \
    | awk -v i="$n" -v t="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      '{print "sample" i, t, "etime", $1, "rss_kb", $2, "pcpu", $3, "threads", $4}' \
    | tee -a "$LOG"
  # Sample every 10s so a 120s soak finishes promptly.
  sleep 10
done
echo "SOAK_END_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ) OK samples=$n" | tee -a "$LOG"
