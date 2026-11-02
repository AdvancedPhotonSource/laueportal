#!/usr/bin/env bash
# Start, stop and inspect a supervisor stack (deploy/supervisor/dev.conf, e2e.conf). The Makefile's dev-up,
# dev-down, dev-status and e2e targets call it.
#
#   scripts/stack.sh up [--seed=missing|always|never] <conf>
#   scripts/stack.sh down <conf>
#   scripts/stack.sh status <conf>       exit status 0 when it runs and is healthy, 1 when it runs but a program
#                                        is not RUNNING or the health check fails, 3 when it does not run (LSB)
#   scripts/stack.sh running <conf>      exit status 0 when the stack's supervisord runs, else 1 (liveness)
#   scripts/stack.sh programs <conf>     the programs `up` starts, one per line
#
# up is idempotent. It starts the stack's supervisord unless it runs (no program starts with it), prepares the
# database (--seed: "missing", the default, runs `laueportal seed demo` when the
# database file does not exist; "always" runs `laueportal seed demo --force`; "never" does nothing), starts
# the other programs and waits until they are ready: /api/v1/health reports the database OK and the worker
# running, the API serves the SPA, and Vite answers. When something does not come up it prints the programs' log
# tails and exits 1, stopping again a supervisord it started.
#
# down asks the stack's supervisord to stop its programs (a worker lets a running run drain for up to
# execution.shutdown_grace_seconds) and waits until it has exited. Neither command ever signals a process
# other than the stack's own supervisord: one found holding a stack port is reported, never killed.
#
# A stack is named by its conf (dev, e2e). Its socket and pidfile are in the workspace's tmp/, its logs in
# its logs/ (<name>-<program>.log). deploy/supervisor/<name>.sh, if present, is sourced before
# seeding and before supervisord starts: the stack's settings overrides (required for e2e, whose database it sets),
# SEED_DEMO_OPTIONS, an array of options `up` gives `laueportal seed demo` (e2e: --crux-run), and prepare_stack, a
# function `up` runs (when the file defines it) before seeding and before the programs start (e2e: its stand-in DM).
# Every command sources the file (as e2e/stack-tools.ts does), so it only sets variables and defines functions.
# One stack runs at a time (they share port 2100, and 2101 with dev's Vite); a stack started from another checkout is
# reported, never touched.
#
# Every program of the conf starts and counts: the remote poller is a thread of the worker, which
# runs it only with remote.enabled (dev.toml's [remote], or LAUEPORTAL_REMOTE__ENABLED).
set -euo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

usage() {
  echo "usage: scripts/stack.sh up [--seed=missing|always|never] <conf> | down <conf> | status <conf> | running <conf> | programs <conf>" >&2
  exit 2
}

action="${1:-}"
[ $# -gt 0 ] && shift
seed=missing
while [ $# -gt 0 ]; do
  case "$1" in
    --seed=*) seed="${1#--seed=}" ;;
    -*) usage ;;
    *) break ;;
  esac
  shift
done
[ $# -eq 1 ] || usage
case "$seed" in missing | always | never) ;; *) usage ;; esac
case "$action" in up | down | status | running | programs) ;; *) usage ;; esac

[ -f "$1" ] || { echo "stack.sh: $1 does not exist" >&2; exit 2; }
CONF="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
NAME="$(basename "$CONF" .conf)"
PROFILE="${CONF%.conf}.sh"
# The e2e stack needs its overrides: without them it would seed and serve the dev database.
if [ "$NAME" = e2e ] && [ ! -f "$PROFILE" ]; then
  echo "stack.sh: $PROFILE is missing: the e2e stack would use the dev settings and database" >&2
  exit 2
fi
SEED_DEMO_OPTIONS=()
# shellcheck disable=SC1090
if [ -f "$PROFILE" ]; then . "$PROFILE"; fi

TMP="$LAUE_DEV_WORKSPACE/tmp"
LOGS="$LAUE_DEV_WORKSPACE/logs"
SOCK="$TMP/supervisor-$NAME.sock"
PIDFILE="$TMP/supervisord-$NAME.pid"
API="http://127.0.0.1:2100"
VITE="http://127.0.0.1:2101"
READY_TIMEOUT="${STACK_READY_TIMEOUT:-180}"   # seconds for the programs to come up
DOWN_TIMEOUT="${STACK_DOWN_TIMEOUT:-720}"     # covers the worker's stopwaitsecs (660)

grep -q "supervisord-$NAME.pid" "$CONF" && grep -q "supervisor-$NAME.sock" "$CONF" || {
  echo "stack.sh: $CONF must use the pidfile $PIDFILE and the socket $SOCK" >&2
  exit 2
}

say() { printf '%s stack: %s\n' "$NAME" "$*"; }
ctl() { supervisorctl -c "$CONF" "$@"; }
programs() { sed -n 's/^\[program:\([^]]*\)\].*/\1/p' "$CONF"; }
has_program() { programs | grep -qx "$1"; }
ports() {
  echo 2100
  if has_program vite; then echo 2101; fi
}

# The PID of stack <name>'s supervisord (from its pidfile, checked against /proc), or nothing (status 1).
# It may have been started from another checkout: its conf path says which (stack_checkout).
live_supervisord() {
  local name="$1" pid
  pid="$(cat "$TMP/supervisord-$name.pid" 2>/dev/null)" || return 1
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  tr '\0' '\n' <"/proc/$pid/cmdline" 2>/dev/null | grep -q "/deploy/supervisor/$name\.conf$" || return 1
  echo "$pid"
}

stack_checkout() {
  tr '\0' '\n' <"/proc/$1/cmdline" | grep -m1 "/deploy/supervisor/$NAME\.conf$" | sed "s#/deploy/supervisor/$NAME\.conf\$##"
}

require_own() {
  local owner
  owner="$(stack_checkout "$1")"
  if [ "$owner" != "$REPO_ROOT" ]; then
    echo "stack.sh: the $NAME stack running (supervisord $1) belongs to the checkout $owner; manage it from there" >&2
    exit 1
  fi
}

listener() { ss -Hltnp "sport = :$1" 2>/dev/null | head -1; }

# /api/v1/health with the database OK and the worker running (its row's newest mark within 45 s).
health_ok() {
  curl -fsS --max-time 3 "$API/api/v1/health" 2>/dev/null | python -c '
import json, sys
health = json.load(sys.stdin)
sys.exit(0 if health["db"] == "ok" and health["worker"] == "running" else 1)'
}

# The API serves the built SPA (index.html) at /.
spa_ok() { curl -fsS --max-time 3 "$API/" 2>/dev/null | grep -q '<div id="root"'; }
vite_ok() { curl -fsS --max-time 3 -o /dev/null "$VITE/" 2>/dev/null; }

program_states() { ctl status 2>/dev/null || true; }
supervisord_answers() { ctl pid >/dev/null 2>&1; }

show_logs() {
  local program
  program_states | sed 's/^/  /'
  for program in $(programs); do
    if [ -f "$LOGS/$NAME-$program.log" ]; then
      echo "--- $LOGS/$NAME-$program.log (last 20 lines)"
      tail -n 20 "$LOGS/$NAME-$program.log"
    fi
  done
}

LAUNCHED=0
fail() {
  echo "stack.sh: $NAME stack: $*" >&2
  show_logs >&2
  if [ "$LAUNCHED" = 1 ]; then
    echo "stack.sh: stopping the $NAME stack it started" >&2
    stop_supervisord || true
  fi
  exit 1
}

# Wait until "$@" succeeds; fail when a program dies for good (FATAL) or after READY_TIMEOUT.
wait_for() {
  local what="$1" deadline=$((SECONDS + READY_TIMEOUT))
  shift
  until "$@"; do
    if program_states | grep -qw FATAL; then
      fail "a program failed to start (FATAL) while waiting for $what"
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      fail "timed out after ${READY_TIMEOUT}s waiting for $what"
    fi
    sleep 1
  done
}

stop_supervisord() {
  local pid deadline
  pid="$(live_supervisord "$NAME")" || return 0
  if ! ctl shutdown >/dev/null 2>&1; then
    # The socket does not answer: signal the stack's own supervisord (checked against /proc above).
    kill -TERM "$pid" 2>/dev/null || true
  fi
  deadline=$((SECONDS + DOWN_TIMEOUT))
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "stack.sh: supervisord $pid did not exit within ${DOWN_TIMEOUT}s" >&2
      return 1
    fi
    sleep 0.5
  done
  rm -f "$SOCK" "$PIDFILE"
}

database_path() {
  python -c 'from laueportal.core.settings import get_settings; print(get_settings().database.path)'
}

up() {
  local pid other port held state program db
  if pid="$(live_supervisord "$NAME")"; then
    require_own "$pid"
    say "supervisord $pid is running"
  else
    for other in "$TMP"/supervisord-*.pid; do
      other="$(basename "$other" .pid)"
      other="${other#supervisord-}"
      if [ "$other" != "$NAME" ] && [ "$other" != "*" ] && pid="$(live_supervisord "$other")"; then
        echo "stack.sh: the $other stack is running (supervisord $pid) on the same ports; stop it first" >&2
        exit 1
      fi
    done
    for port in $(ports); do
      held="$(listener "$port")"
      if [ -n "$held" ]; then
        echo "stack.sh: port $port is in use, not by the $NAME stack; it is left alone:" >&2
        echo "  $held" >&2
        exit 1
      fi
    done
    mkdir -p "$TMP" "$LOGS"
    rm -f "$SOCK" "$PIDFILE"
    # The stack's logs are private: the conf's umask (077) covers the files supervisord creates once it runs,
    # this one its own log (opened before), and chmod the logs an earlier stack created.
    chmod go-rwx "$LOGS/$NAME"-*.log* "$LOGS/supervisord-$NAME".log* 2>/dev/null || true
    say "starting supervisord ($CONF)"
    (umask 077 && supervisord -c "$CONF")
    LAUNCHED=1
    wait_for "supervisord to answer" supervisord_answers
  fi

  db="$(database_path)" || fail "could not read the settings (laueportal config show)"
  if declare -F prepare_stack >/dev/null; then
    say "preparing the stack (prepare_stack, $PROFILE)"
    prepare_stack || fail "prepare_stack failed"
  fi
  case "$seed" in
    always)
      say "seeding the demo database $db"
      laueportal seed demo --force "${SEED_DEMO_OPTIONS[@]}" || fail "laueportal seed demo --force failed"
      ;;
    missing)
      if [ ! -e "$db" ]; then
        say "the database $db does not exist: seeding the demo (laueportal seed demo)"
        laueportal seed demo "${SEED_DEMO_OPTIONS[@]}" || fail "laueportal seed demo failed"
      fi
      ;;
  esac
  [ -e "$db" ] || fail "the database $db does not exist (laueportal seed demo, or laueportal db upgrade)"

  for program in $(programs); do
    state="$(ctl status "$program" 2>/dev/null | awk '{print $2}')" || true
    case "$state" in
      RUNNING | STARTING | BACKOFF) ;;   # running, or supervisor is (re)starting it
      *) ctl start "$program" >/dev/null 2>&1 || fail "$program did not start" ;;
    esac
  done
  wait_for "the API to report the database OK and the worker running ($API/api/v1/health)" health_ok
  wait_for "the API to serve the SPA ($API/; make build)" spa_ok
  if has_program vite; then
    wait_for "Vite on $VITE" vite_ok
  fi

  say "up"
  echo "  $API   API and the built SPA"
  if has_program vite; then echo "  $VITE   Vite dev server (hot reload)"; fi
  echo "  logs: $LOGS/$NAME-*.log"
}

down() {
  local pid port held status=0
  if ! pid="$(live_supervisord "$NAME")"; then
    rm -f "$SOCK" "$PIDFILE"
    say "not running"
    return 0
  fi
  require_own "$pid"
  say "stopping (supervisord $pid; a running run may take up to execution.shutdown_grace_seconds to drain)"
  stop_supervisord || exit 1
  for port in $(ports); do
    held="$(listener "$port")"
    if [ -n "$held" ]; then
      echo "stack.sh: port $port is still in use; stack.sh did not start that process and leaves it alone:" >&2
      echo "  $held" >&2
      status=1
    fi
  done
  [ "$status" = 0 ] && say "stopped"
  return "$status"
}

status() {
  local pid health ok=0
  if ! pid="$(live_supervisord "$NAME")"; then
    say "not running"
    return 3   # LSB: program is not running
  fi
  say "supervisord $pid (checkout $(stack_checkout "$pid"))"
  program_states | sed 's/^/  /'
  # Every program must run.
  if program_states | awk '$2 != "RUNNING" { bad = 1 } END { exit !bad }'; then ok=1; fi
  if health="$(curl -fsS --max-time 3 "$API/api/v1/health" 2>/dev/null)"; then
    printf '%s' "$health" | python -c '
import json, sys
health = json.load(sys.stdin)
worker = (health["worker"] or "unknown").replace("_", " ")
print("  health: db %s, worker %s; version %s" % (health["db"], worker, health["version"]))
sys.exit(0 if health["db"] == "ok" and health["worker"] == "running" else 1)' || ok=1
  else
    echo "  health: $API/api/v1/health does not answer"
    ok=1
  fi
  return "$ok"
}

case "$action" in
  up) up ;;
  down) down ;;
  status) status ;;
  running) live_supervisord "$NAME" >/dev/null ;;
  programs) programs ;;
esac
