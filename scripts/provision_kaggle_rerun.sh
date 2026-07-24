#!/usr/bin/env bash
# Provision the one fresh Kaggle JIT runner needed by a same-databaseId rerun.
#
# A GitHub "re-run failed jobs" keeps the relink workflow databaseId and inputs, but
# the previous JIT runner is intentionally one-shot.  The normal intent provisioner
# must not dispatch a second child, so this scanner handles only queued attempts > 1
# and attaches a new request-isolated runner to that exact existing job.
set -euo pipefail

REPO=${GITHUB_REPOSITORY:-Otzaria/LinkerToOtzaria}
KERNEL_ID=${KAGGLE_KERNEL_ID:-otzaria/linker-gpu-runner}
TOOLS_KERNEL=${KAGGLE_TOOLS_KERNEL:-otzaria/linker-tools-fetcher}
RUNTIME_KERNEL=${KAGGLE_RUNTIME_KERNEL:-otzaria/linker-python-runtime}
HERE=$(cd "$(dirname "$0")/.." && pwd)
EXPLICIT_RUN_ID=""

while [ $# -gt 0 ]; do
  case "$1" in
    --run-id) EXPLICIT_RUN_ID="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -z "$EXPLICIT_RUN_ID" ] || [[ "$EXPLICIT_RUN_ID" =~ ^[1-9][0-9]*$ ]] || {
  echo "--run-id must be a positive integer" >&2
  exit 2
}

TMP=$(mktemp -d)
JIT_NAME=""
cleanup() {
  local ec=$?
  rm -rf "$TMP"
  if [ "$ec" -ne 0 ] && [ -n "$JIT_NAME" ]; then
    runner_id=$(gh api --paginate -X GET "repos/$REPO/actions/runners" -f per_page=100 \
      --jq ".runners[] | select(.name==\"$JIT_NAME\" and .busy==false) | .id" 2>/dev/null | head -1 || true)
    [ -z "$runner_id" ] || gh api -X DELETE "repos/$REPO/actions/runners/$runner_id" >/dev/null 2>&1 || true
  fi
  exit "$ec"
}
trap cleanup EXIT

if [ -n "$EXPLICIT_RUN_ID" ]; then
  candidates="$EXPLICIT_RUN_ID"
else
  candidates=$(gh api --paginate -X GET "repos/$REPO/actions/workflows/relink.yml/runs" \
    -f event=workflow_dispatch -f status=queued -f per_page=100 \
    --jq '.workflow_runs[] | select(.run_attempt > 1) | (.id|tostring)')
fi
candidate_count=$(printf '%s\n' "$candidates" | awk 'NF' | wc -l | tr -d ' ')
if [ "$candidate_count" -eq 0 ]; then
  echo "no queued same-id Kaggle rerun needs provisioning"
  exit 0
fi
[ "$candidate_count" -eq 1 ] || {
  echo "::error::$candidate_count queued same-id relink reruns found; refusing to choose"
  exit 1
}
RUN_ID=$(printf '%s\n' "$candidates" | awk 'NF {print; exit}')

gh api "repos/$REPO/actions/runs/$RUN_ID" > "$TMP/run.json"
REQUEST_ID=$(python3 - "$TMP/run.json" <<'PY'
import json, re, sys
v=json.load(open(sys.argv[1]))
if v.get("event")!="workflow_dispatch" or v.get("path")!=".github/workflows/relink.yml":
    raise SystemExit("run is not a workflow_dispatch relink")
if v.get("status")!="queued" or v.get("conclusion") is not None:
    raise SystemExit("run is not queued")
attempt=v.get("run_attempt")
if type(attempt) is not int or attempt <= 1:
    raise SystemExit("run is not a same-databaseId rerun")
m=re.fullmatch(r"(?:relink|relink-recovery) request=([0-9a-f]{64}) parent=(?:standalone|[1-9][0-9]*:[1-9][0-9]*)",
               v.get("display_title",""))
if not m:
    raise SystemExit("run title does not carry an exact relink identity")
print(m.group(1))
PY
)
RUN_ATTEMPT=$(jq -r .run_attempt "$TMP/run.json")
HEAD_SHA=$(jq -r .head_sha "$TMP/run.json")

# The rerun may use the immediately preceding release commit while this scanner
# comes from a newer hardening commit.  It must still be on main's ancestry.
compare_status=$(gh api "repos/$REPO/compare/$HEAD_SHA...main" --jq .status)
case "$compare_status" in
  identical|ahead) ;;
  *) echo "::error::rerun head $HEAD_SHA is not an ancestor of main ($compare_status)"; exit 1 ;;
esac

gh api "repos/$REPO/actions/runs/$RUN_ID/attempts/$RUN_ATTEMPT/jobs" > "$TMP/jobs.json"
python3 - "$TMP/jobs.json" "$REQUEST_ID" <<'PY'
import json,sys
jobs=json.load(open(sys.argv[1])).get("jobs",[])
expected=["self-hosted","kaggle","gpu","request-"+sys.argv[2]]
if len(jobs)!=1:
    raise SystemExit(f"rerun exposes {len(jobs)} jobs before pickup (expected one)")
j=jobs[0]
if j.get("name")!="relink" or j.get("status")!="queued" or j.get("runner_name"):
    raise SystemExit("exact relink job is not waiting unclaimed")
if j.get("labels")!=expected:
    raise SystemExit("queued relink job labels do not match the request-isolated contract")
PY

# Exactly this run may be active. Any second relink means the singleton invariant is
# already broken and provisioning a GPU would make it worse.
active_ids=$(gh api --paginate -X GET "repos/$REPO/actions/workflows/relink.yml/runs" \
  -f per_page=100 --jq '.workflow_runs[] | select(.status!="completed") | (.id|tostring)')
other_active=$(printf '%s\n' "$active_ids" | awk -v id="$RUN_ID" 'NF && $0!=id {n++} END{print n+0}')
[ "$other_active" -eq 0 ] || {
  echo "::error::$other_active other relink run(s) are active; refusing to provision"
  exit 1
}

kernel_status=$(kaggle kernels status "$KERNEL_ID")
case "$kernel_status" in
  *COMPLETE*|*ERROR*|*CANCELLED*) ;;
  *RUNNING*|*QUEUED*|*INITIALIZING*)
    echo "Kaggle kernel is already active; leaving exact queued rerun unchanged"
    exit 0
    ;;
  *) echo "::error::unknown Kaggle kernel status: $kernel_status"; exit 1 ;;
esac

stale=$(gh api --paginate -X GET "repos/$REPO/actions/runners" -f per_page=100 \
  --jq '.runners[] | select(.name|startswith("kaggle-")) | [.id,.name,.busy] | @tsv')
while IFS=$'\t' read -r runner_id runner_name runner_busy; do
  [ -n "$runner_id" ] || continue
  [ "$runner_busy" = false ] || {
    echo "::error::Kaggle runner $runner_name is unexpectedly busy"
    exit 1
  }
  gh api -X DELETE "repos/$REPO/actions/runners/$runner_id"
done <<< "$stale"

JIT_NAME="kaggle-${REQUEST_ID:0:16}-rerun${RUN_ATTEMPT}-$(date +%Y%m%d-%H%M%S)"
JIT_CONFIG=$(gh api -X POST "repos/$REPO/actions/runners/generate-jitconfig" \
  -f name="$JIT_NAME" -F runner_group_id=1 -f work_folder=_work \
  -f 'labels[]=self-hosted' -f 'labels[]=kaggle' -f 'labels[]=gpu' \
  -f "labels[]=request-$REQUEST_ID" -q .encoded_jit_config)

mkdir "$TMP/kernel"
RUNNER_JIT_CONFIG="$JIT_CONFIG" BOOTSTRAP_PATH="$HERE/kaggle/bootstrap.sh" \
KERNEL_DIR="$TMP/kernel" KERNEL_ID="$KERNEL_ID" TOOLS_KERNEL="$TOOLS_KERNEL" \
RUNTIME_KERNEL="$RUNTIME_KERNEL" python3 - <<'PY'
import base64,json,os
from pathlib import Path
root=Path(os.environ["KERNEL_DIR"])
bootstrap=base64.b64encode(Path(os.environ["BOOTSTRAP_PATH"]).read_bytes()).decode()
jit=os.environ["RUNNER_JIT_CONFIG"]
(root/"run.py").write_text(
    "import base64, os, subprocess\n"
    "os.makedirs('/kaggle/temp', exist_ok=True)\n"
    f"open('/kaggle/temp/bootstrap.sh','wb').write(base64.b64decode({bootstrap!r}))\n"
    f"subprocess.run(['bash','/kaggle/temp/bootstrap.sh'], env=dict(os.environ,JIT_CONFIG={jit!r}), check=True)\n"
)
(root/"kernel-metadata.json").write_text(json.dumps({
    "id":os.environ["KERNEL_ID"],"title":"linker-gpu-runner","code_file":"run.py",
    "language":"python","kernel_type":"script","is_private":True,"enable_gpu":True,
    "enable_tpu":False,"enable_internet":True,"dataset_sources":[],
    "competition_sources":[],"kernel_sources":[os.environ["TOOLS_KERNEL"],os.environ["RUNTIME_KERNEL"]],
    "model_sources":[]
},sort_keys=True,separators=(",",":"))+"\n")
PY

push_output=$(kaggle kernels push -p "$TMP/kernel" 2>&1)
printf '%s\n' "$push_output"
if printf '%s\n' "$push_output" | grep -q 'Kernel push error:' ||
   ! printf '%s\n' "$push_output" | grep -Eq 'Kernel version [0-9]+ successfully pushed'; then
  echo "::error::Kaggle did not accept the kernel push"
  exit 1
fi
echo "provisioned exact rerun $RUN_ID attempt $RUN_ATTEMPT on $JIT_NAME"
JIT_NAME=""
