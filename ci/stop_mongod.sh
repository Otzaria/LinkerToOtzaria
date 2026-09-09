#!/usr/bin/env bash
# Stop the mongod THIS run started or adopted — on every exit path.
#
# setup_stack.sh starts it with --fork, so it daemonizes into its own session and the
# GitHub runner sees it only at "Complete job": run 34015274945 printed `Terminate
# orphan process: pid (1413) (mongod)` there. That generic reaper runs only on a CLEAN
# exit — a runner death (attempt 2 of the same cycle) or a hard cancel leaves the
# daemon behind with nothing recording that it was ours.
#
# Two guards, because the host is shared and PIDs are reused: the process-scope state
# proves the pid is still the same mongod (start time + uid + cmdline, not a bare
# pidfile), and the owner marker proves the daemon belongs to this run — a cleanup step
# that runs after the host lease was already released must never stop the mongod of
# whichever run took the lease next.
#
# Always exits 0 and always prints exactly one line: hygiene must not turn a good
# relink red, and silence must not hide a leak.
set -euo pipefail
CACHE="${LINKER_CACHE_DIR:-$HOME/.cache/linker-stack}"
STATE="${LINKER_MONGO_SCOPE:-$CACHE/mongod.scope.json}"
OWNER_FILE="$CACHE/mongod.owner"
OWNER="${LINKER_MONGO_OWNER:-${GITHUB_RUN_ID:-manual}:${GITHUB_RUN_ATTEMPT:-0}}"
HERE=$(cd "$(dirname "$0")" && pwd)

if [ ! -f "$STATE" ]; then
  echo "mongod: not running"
  exit 0
fi
RECORDED_OWNER="$(cat "$OWNER_FILE" 2>/dev/null || true)"
if [ -n "$RECORDED_OWNER" ] && [ "$RECORDED_OWNER" != "$OWNER" ]; then
  echo "mongod: owned by $RECORDED_OWNER, not $OWNER — left running"
  exit 0
fi
PID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["identity"]["pid"])' \
    "$STATE" 2>/dev/null || echo unknown)"
if ! OUTPUT="$(python3 "$HERE/process_scope.py" terminate --state "$STATE" \
    --expect "--dbpath $CACHE/mongo-data" --grace 30 2>&1)"; then
  echo "::warning::mongod: could not stop pid $PID: $(printf '%s' "$OUTPUT" | tr '\n' ' ')"
  exit 0
fi
rm -f "$OWNER_FILE" "$CACHE/mongod.pid"
case "$OUTPUT" in
  terminated*) echo "mongod: stopped pid $PID" ;;
  *) echo "mongod: not running" ;;
esac
