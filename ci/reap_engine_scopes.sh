#!/usr/bin/env bash
# Reap only engine process groups recorded by a terminal prior workflow.  The
# caller must be serialized against every server relink; process_scope.py then
# validates PID start-time/uid/cmdline before signalling any group.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
SCOPE_DIR="${LINKER_ENGINE_SCOPE_DIR:-$HOME/.cache/linker-stack/engine-scopes}"
mkdir -p "$SCOPE_DIR"
shopt -s nullglob
for state in "$SCOPE_DIR"/engine-*.json; do
  python3 "$HERE/process_scope.py" terminate --state "$state" --expect link_books.py --grace 15
done
