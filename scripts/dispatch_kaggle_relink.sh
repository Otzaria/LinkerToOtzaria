#!/usr/bin/env bash
# Dispatch a relink onto a fresh Kaggle GPU session.
#
#   scripts/dispatch_kaggle_relink.sh [--dry-run] [--library-run-id <id>]
#
# --library-run-id: serial mode — the relink takes lines_snapshot.db.zst from that
# SeforimLibrary run and the waiting build downloads the artifacts back from it.
#
# Order matters: the relink job is queued FIRST (runs-on [self-hosted, kaggle, gpu] —
# it just waits), THEN the kernel is pushed; the session boots, registers as a one-job
# JIT runner and picks the job up. Requires: gh authenticated with repo-admin rights,
# kaggle CLI authenticated (both true on the operator machine and in kaggle-relink.yml),
# nothing else — the kernel is self-contained (bootstrap.sh + JIT config embedded into
# run.py) and pulls its pinned tool binaries from the otzaria/linker-runner-tools-v3 dataset.
set -euo pipefail
REPO=Otzaria/LinkerToOtzaria
KERNEL_ID=otzaria/linker-gpu-runner
TOOLS_DATASET=otzaria/linker-runner-tools-v3
HERE=$(cd "$(dirname "$0")/.." && pwd)

DRY=""
LIBRARY_RUN_ID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --library-run-id) LIBRARY_RUN_ID="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

NAME="kaggle-$(date +%Y%m%d-%H%M%S)"
JIT=$(gh api -X POST "repos/$REPO/actions/runners/generate-jitconfig" \
  -f name="$NAME" -F runner_group_id=1 -f work_folder=_work \
  -f 'labels[]=self-hosted' -f 'labels[]=kaggle' -f 'labels[]=gpu' \
  -q .encoded_jit_config)
echo "JIT runner registered: $NAME"

gh workflow run relink.yml -R "$REPO" -f target=kaggle \
  ${LIBRARY_RUN_ID:+-f library_run_id="$LIBRARY_RUN_ID"} \
  ${DRY:+-f dry_run=true}
echo "relink queued (target=kaggle${LIBRARY_RUN_ID:+, library_run_id=$LIBRARY_RUN_ID}${DRY:+, dry_run})"

PUSH=$(mktemp -d)
B64=$(base64 < "$HERE/kaggle/bootstrap.sh" | tr -d '\n')
cat > "$PUSH/run.py" <<PY
import base64, os, subprocess
os.makedirs("/kaggle/temp", exist_ok=True)
with open("/kaggle/temp/bootstrap.sh", "w") as fh:
    fh.write(base64.b64decode("$B64").decode())
env = dict(os.environ, JIT_CONFIG="$JIT")
subprocess.run(["bash", "/kaggle/temp/bootstrap.sh"], env=env, check=True)
PY
cat > "$PUSH/kernel-metadata.json" <<JSON
{
  "id": "$KERNEL_ID",
  "title": "linker-gpu-runner",
  "code_file": "run.py",
  "language": "python",
  "kernel_type": "script",
  "is_private": true,
  "enable_gpu": true,
  "enable_tpu": false,
  "enable_internet": true,
  "dataset_sources": ["$TOOLS_DATASET"],
  "competition_sources": [],
  "kernel_sources": [],
  "model_sources": []
}
JSON
kaggle kernels push -p "$PUSH"
rm -rf "$PUSH"

echo
echo "session status:  kaggle kernels status $KERNEL_ID"
echo "session log:     kaggle kernels output $KERNEL_ID -p /tmp/kaggle-log --force"
echo "job pickup:      gh run list -R $REPO -w relink.yml -L1"
