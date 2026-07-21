#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir "$TMP/bin"

cat > "$TMP/bin/gh" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = run ] && [ "$2" = download ]; then
  run_id="$3"; shift 3; dir=""
  while [ $# -gt 0 ]; do
    case "$1" in -D) dir="$2"; shift 2;; *) shift;; esac
  done
  mkdir -p "$dir"
  printf '%s' "$run_id" > "$MOCK_STATE"
  RUN_ID="$run_id" OUT="$dir" python3 - <<'PY'
import hashlib,json,os
from pathlib import Path
run=int(os.environ['RUN_ID']); request=('a' if run==101 else ('c' if run==103 else 'b'))*64
recovery=bool(os.environ.get('RECOVERY_INTENT'))
v={'schema_version':(2 if recovery else (True if os.environ.get('MALFORMED_INTENT') else 1)),'request_id':request,'library_run_id':('777' if recovery else ''),'parent_run_attempt':('2' if recovery else ''),'sefaria_tag':('sefaria-pin' if recovery else ''),'snapshot_sha256':('d'*64 if recovery else ''),'sefaria_release_metadata_sha256':('e'*64 if recovery else ''),'dry_run':False,'intake_run_id':run,'intake_run_attempt':1}
if recovery: v['recovery_mode']=True
raw=(json.dumps(v,sort_keys=True,separators=(',',':'))+'\n').encode(); root=Path(os.environ['OUT'])
(root/'kaggle-intent.json').write_bytes(raw); (root/'kaggle-intent.sha256').write_text(hashlib.sha256(raw).hexdigest()+'\n')
PY
  exit 0
fi
if [ "$1" = api ]; then
  joined="$*"
  if [[ "$joined" == *"actions/workflows/kaggle-relink.yml/runs"* ]]; then
    printf '102\n101\n'; exit 0
  fi
  if [[ "$joined" =~ actions/runs/([0-9]+)/artifacts ]]; then
    if [[ "$joined" == *"actions/runs/777/artifacts"* ]]; then printf '4242\n'; exit 0; fi
    printf 'kaggle-intent-%064d-1\n' 0; exit 0
  fi
  if [[ "$joined" == *"actions/workflows/relink.yml/runs"* ]]; then
    current=$(cat "$MOCK_STATE" 2>/dev/null || true)
    if [ "$current" = 101 ] && [[ "$joined" != *"status="* ]]; then
      request=$(printf '%064d' 0 | tr 0 a)
      printf '9001\tcompleted\trelink request=%s parent=standalone\tdeadbeef\t2026-01-01T00:00:00Z\n' "$request"
    fi
    exit 0
  fi
  if [[ "$joined" =~ actions/runs/([0-9]+) ]]; then
    if [[ "$joined" == *"actions/runs/777"* ]]; then
      printf '{"status":"completed","run_attempt":2,"conclusion":"%s"}\n' "${PARENT_CONCLUSION:-failure}"
      exit 0
    fi
    if [[ "$joined" == *"--jq .conclusion"* ]]; then printf 'success\n'; else printf '1\n'; fi
    exit 0
  fi
fi
echo "unexpected gh call: $*" >&2
exit 90
MOCK

cat > "$TMP/fake-dispatch.sh" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" > "$DISPATCH_LOG"
MOCK
chmod +x "$TMP/bin/gh" "$TMP/fake-dispatch.sh"

PATH="$TMP/bin:$PATH" MOCK_STATE="$TMP/state" DISPATCH_LOG="$TMP/dispatch" \
  KAGGLE_DISPATCH_SCRIPT="$TMP/fake-dispatch.sh" \
  GITHUB_REPOSITORY=Otzaria/LinkerToOtzaria \
  /bin/bash "$ROOT/scripts/provision_kaggle_intent.sh"

test -s "$TMP/dispatch"
grep -q -- "--relink-request-id $(printf '%064d' 0 | tr 0 b)" "$TMP/dispatch"
echo "ok   completed oldest intent skipped; next pending intent dispatched"

rm -f "$TMP/dispatch"
rc=0
PATH="$TMP/bin:$PATH" MOCK_STATE="$TMP/state" DISPATCH_LOG="$TMP/dispatch" \
  KAGGLE_DISPATCH_SCRIPT="$TMP/fake-dispatch.sh" MALFORMED_INTENT=1 INTENT_RUN_ID=102 \
  GITHUB_REPOSITORY=Otzaria/LinkerToOtzaria \
  /bin/bash "$ROOT/scripts/provision_kaggle_intent.sh" >/dev/null 2>&1 || rc=$?
test "$rc" -ne 0
test ! -e "$TMP/dispatch"
echo "ok   boolean schema_version is rejected before provisioning"

rm -f "$TMP/dispatch"
PATH="$TMP/bin:$PATH" MOCK_STATE="$TMP/state" DISPATCH_LOG="$TMP/dispatch" \
  KAGGLE_DISPATCH_SCRIPT="$TMP/fake-dispatch.sh" RECOVERY_INTENT=1 INTENT_RUN_ID=103 \
  GITHUB_REPOSITORY=Otzaria/LinkerToOtzaria \
  /bin/bash "$ROOT/scripts/provision_kaggle_intent.sh"
test -s "$TMP/dispatch"
grep -q -- "--relink-request-id $(printf '%064d' 0 | tr 0 c)" "$TMP/dispatch"
grep -q -- "--library-run-id 777 --parent-run-attempt 2" "$TMP/dispatch"
echo "ok   exact failed terminal parent with one snapshot can be recovered"

rm -f "$TMP/dispatch"
PATH="$TMP/bin:$PATH" MOCK_STATE="$TMP/state" DISPATCH_LOG="$TMP/dispatch" \
  KAGGLE_DISPATCH_SCRIPT="$TMP/fake-dispatch.sh" RECOVERY_INTENT=1 PARENT_CONCLUSION=success INTENT_RUN_ID=103 \
  GITHUB_REPOSITORY=Otzaria/LinkerToOtzaria \
  /bin/bash "$ROOT/scripts/provision_kaggle_intent.sh" >/dev/null
test ! -e "$TMP/dispatch"
echo "ok   successful terminal parent cannot enter recovery mode"

python3 - "$ROOT/.github/workflows/kaggle-provisioner.yml" <<'PY'
import sys
from pathlib import Path
workflow = Path(sys.argv[1]).read_text(encoding="utf-8")
job = workflow.split("  provision:\n", 1)[1]
if "    concurrency:\n      group: linker-relink\n      cancel-in-progress: false" not in job:
    raise SystemExit("provisioner does not hold the real relink mutex")
header = workflow.split("jobs:\n", 1)[0]
if "queue: max" in header:
    raise SystemExit("interchangeable provisioner ticks must not accumulate as durable intents")
PY
echo "ok   provisioner holds relink mutex through admission and dispatch"
