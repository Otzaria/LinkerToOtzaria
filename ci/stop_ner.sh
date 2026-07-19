#!/usr/bin/env bash
# Stop the pipeline-owned NER process group using a start-time-bound identity.
# A raw pidfile is deliberately insufficient because PIDs are reused.
set -euo pipefail
CACHE="${LINKER_CACHE_DIR:-$HOME/.cache/linker-stack}"
STATE="${LINKER_NER_SCOPE:-$CACHE/gunicorn.scope.json}"
HERE=$(cd "$(dirname "$0")" && pwd)
python3 "$HERE/process_scope.py" terminate --state "$STATE" --expect 'app:create_app()' --grace 20
rm -f "$CACHE/gunicorn.pid" "$CACHE/.ner-identity"
