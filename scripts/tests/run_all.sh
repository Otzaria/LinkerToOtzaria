#!/usr/bin/env bash
# Run every offline acceptance test (mocked gh/kaggle — no network, no tokens).
# These lock the dispatcher-cleanup and reconciler contracts; canary-only checks
# (queue:max under 3 real dispatches, cross-attempt request ids, reconciler on real
# infrastructure) are listed in each test's header and exercised at rollout.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
rc=0
for t in "$HERE"/test_*.sh; do
  echo "=== $(basename "$t") ==="
  bash "$t" || rc=1
done
exit "$rc"
