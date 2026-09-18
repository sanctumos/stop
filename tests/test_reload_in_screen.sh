#!/usr/bin/env bash
# Shell tests for reload-in-screen.sh with a fake `screen` (#4071).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FAKE="$(mktemp -d)"
trap 'rm -rf "$FAKE"' EXIT

cat >"$FAKE/screen" <<'EOS'
#!/usr/bin/env bash
# Fake screen: reads STOP_FAKE_LS / logs -X args.
cmd="${1:-}"
if [[ "$cmd" == "-ls" ]]; then
  cat "${STOP_FAKE_LS:-/dev/null}"
  exit 0
fi
echo "$*" >>"${STOP_FAKE_LOG:-/tmp/fake-screen.log}"
# Reject destroy commands for the test harness itself.
case " $* " in
  *" -X quit "*|*" -X kill "*|*" kill "*)
    echo "FORBIDDEN: $*" >&2
    exit 99
    ;;
esac
exit 0
EOS
chmod +x "$FAKE/screen"

export PATH="$FAKE:$PATH"
export STOP_REPO="$ROOT"
LOG="$FAKE/log"
export STOP_FAKE_LOG="$LOG"
: >"$LOG"

pass=0
fail=0
check() {
  local name="$1" expect="$2"
  shift 2
  set +e
  out="$("$@" 2>&1)"
  rc=$?
  set -e
  if [[ "$rc" -eq "$expect" ]]; then
    echo "PASS $name (rc=$rc)"
    pass=$((pass + 1))
  else
    echo "FAIL $name expected rc=$expect got $rc"
    echo "$out"
    fail=$((fail + 1))
  fi
}

# missing
export STOP_FAKE_LS="$FAKE/ls-empty"
: >"$STOP_FAKE_LS"
check missing 2 "$ROOT/tools/reload-in-screen.sh"

# ambiguous
export STOP_FAKE_LS="$FAKE/ls-ambig"
printf '%s\n' \
  "  111.stop   (01/01/2026)   (Detached)" \
  "  222.stop   (01/01/2026)   (Detached)" \
  >"$STOP_FAKE_LS"
check ambiguous 3 "$ROOT/tools/reload-in-screen.sh"

# attached skip
export STOP_FAKE_LS="$FAKE/ls-att"
printf '%s\n' "  333.stop   (01/01/2026)   (Attached)" >"$STOP_FAKE_LS"
: >"$LOG"
check attached-skip 0 "$ROOT/tools/reload-in-screen.sh"
if grep -q stuff "$LOG"; then
  echo "FAIL attached-skip must not stuff keys"
  fail=$((fail + 1))
else
  echo "PASS attached-skip no stuff"
  pass=$((pass + 1))
fi

# detached reload (force path; pgrep will miss → exit 1 WARN is ok)
export STOP_FAKE_LS="$FAKE/ls-det"
printf '%s\n' "  444.stop   (01/01/2026)   (Detached)" >"$STOP_FAKE_LS"
: >"$LOG"
set +e
"$ROOT/tools/reload-in-screen.sh" >/tmp/reload-out.$$ 2>&1
rc=$?
set -e
if grep -q "FORBIDDEN" /tmp/reload-out.$$; then
  echo "FAIL detached used destroy command"
  fail=$((fail + 1))
elif ! grep -q "stuff" "$LOG"; then
  echo "FAIL detached should stuff q + start"
  cat "$LOG"
  fail=$((fail + 1))
elif grep -E -- '-X quit|-X kill' "$LOG"; then
  echo "FAIL detached logged quit/kill"
  fail=$((fail + 1))
else
  echo "PASS detached stuff only (rc=$rc, warn-ok)"
  pass=$((pass + 1))
fi
rm -f /tmp/reload-out.$$

# source must never contain destroy verbs targeting the session
if grep -EEq -- '-X[[:space:]]+(quit|kill)|screen[[:space:]]+-wipe|killall[[:space:]]+screen' \
  "$ROOT/tools/reload-in-screen.sh"; then
  echo "FAIL reload script contains session-destroy command"
  fail=$((fail + 1))
else
  echo "PASS reload script has no session-destroy commands"
  pass=$((pass + 1))
fi

echo "reload-shell-tests: $pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
