#!/usr/bin/env bash
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib/gh_runs.sh
source "$HERE/lib/gh_runs.sh"
REPO=${GITHUB_REPOSITORY:-Otzaria/LinkerToOtzaria}
PARENT_REPO=${KAGGLE_PARENT_REPO:-Otzaria/SeforimLibrary}
DISPATCH_SCRIPT=${KAGGLE_DISPATCH_SCRIPT:-$HERE/dispatch_kaggle_relink.sh}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

if [ -n "${INTENT_RUN_ID:-}" ]; then
  [[ "$INTENT_RUN_ID" =~ ^[1-9][0-9]*$ ]]
  candidates="$INTENT_RUN_ID"
else
  since=$(python3 - <<'PY'
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
  )
  candidates=$(gh api --paginate -X GET "repos/$REPO/actions/workflows/kaggle-relink.yml/runs" \
    -f event=workflow_dispatch -f status=completed -f created=">=$since" -f per_page=100 \
    --jq '.workflow_runs[] | select(.conclusion=="success") | (.id|tostring)' | \
    awk '{rows[NR]=$0} END {for (i=NR; i>=1; i--) print rows[i]}')
fi

for intake_run in $candidates; do
  rm -rf "$TMP/intent" && mkdir "$TMP/intent"
  intake_attempt=$(gh api "repos/$REPO/actions/runs/$intake_run" --jq .run_attempt)
  [[ "$intake_attempt" =~ ^[1-9][0-9]*$ ]] || { echo "::error::invalid intake attempt for $intake_run"; exit 1; }
  artifacts=$(gh api --paginate "repos/$REPO/actions/runs/$intake_run/artifacts?per_page=100" \
    --jq '.artifacts[] | select(.expired==false and (.name|startswith("kaggle-intent-"))) | .name')
  artifact=$(printf '%s\n' "$artifacts" | awk -v suffix="-$intake_attempt" 'length($0)>=length(suffix) && substr($0,length($0)-length(suffix)+1)==suffix')
  artifact_count=$(printf '%s\n' "$artifact" | awk 'NF' | wc -l | tr -d ' ')
  if [ "$artifact_count" -ne 1 ]; then
    echo "::error::successful Kaggle intake $intake_run has $artifact_count intent artifacts (expected exactly one)"
    exit 1
  fi
  gh run download "$intake_run" -R "$REPO" -n "$artifact" -D "$TMP/intent"
  python3 - "$TMP/intent" "$intake_run" "$intake_attempt" <<'PY'
import hashlib,json,re,sys
from pathlib import Path
root=Path(sys.argv[1]); expected_run=int(sys.argv[2]); expected_attempt=int(sys.argv[3]); path=root/'kaggle-intent.json'
def pairs(items):
 out={}
 for k,v in items:
  if k in out: raise SystemExit(f'duplicate intent key {k}')
  out[k]=v
 return out
v=json.loads(path.read_text(),object_pairs_hook=pairs)
keys={'schema_version','request_id','library_run_id','parent_run_attempt','sefaria_tag','snapshot_sha256','sefaria_release_metadata_sha256','dry_run','intake_run_id','intake_run_attempt'}
if set(v)!=keys or type(v['schema_version']) is not int or v['schema_version']!=1 or type(v['intake_run_id']) is not int or v['intake_run_id']!=expected_run: raise SystemExit('invalid intent schema/identity')
string_fields=('request_id','library_run_id','parent_run_attempt','sefaria_tag','snapshot_sha256','sefaria_release_metadata_sha256')
if any(type(v[k]) is not str for k in string_fields): raise SystemExit('invalid intent string types')
if not re.fullmatch(r'[0-9a-f]{64}',v['request_id']): raise SystemExit('invalid request id')
if type(v['dry_run']) is not bool or type(v['intake_run_attempt']) is not int or v['intake_run_attempt'] != expected_attempt: raise SystemExit('invalid intent types/attempt')
serial=bool(v['library_run_id'])
if serial:
 if not re.fullmatch(r'[1-9][0-9]*',v['library_run_id']) or not re.fullmatch(r'[1-9][0-9]*',v['parent_run_attempt']): raise SystemExit('invalid serial parent identity')
 if not re.fullmatch(r'[A-Za-z0-9._-]{1,100}',v['sefaria_tag']): raise SystemExit('invalid serial Sefaria tag')
 if not re.fullmatch(r'[0-9a-f]{64}',v['snapshot_sha256']) or not re.fullmatch(r'[0-9a-f]{64}',v['sefaria_release_metadata_sha256']): raise SystemExit('invalid serial pin digests')
elif v['parent_run_attempt']:
 raise SystemExit('standalone intent must not name a parent attempt')
raw=path.read_bytes(); side=(root/'kaggle-intent.sha256').read_bytes()
if side != (hashlib.sha256(raw).hexdigest()+'\n').encode(): raise SystemExit('intent sidecar mismatch')
canonical=(json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
if raw != canonical: raise SystemExit('intent is not canonical JSON')
PY
  request_id=$(jq -r .request_id "$TMP/intent/kaggle-intent.json")
  library_run_id=$(jq -r .library_run_id "$TMP/intent/kaggle-intent.json")
  parent_attempt=$(jq -r .parent_run_attempt "$TMP/intent/kaggle-intent.json")
  if [ -n "$library_run_id" ]; then
    if ! gh api "repos/$PARENT_REPO/actions/runs/$library_run_id" > "$TMP/parent.json" 2> "$TMP/parent.err"; then
      if grep -q 'HTTP 404' "$TMP/parent.err"; then
        echo "serial intent $request_id has no parent run; leaving it unprovisioned"
        continue
      fi
      echo "::error::cannot verify parent of serial Kaggle intent $request_id"
      cat "$TMP/parent.err" >&2
      exit 1
    fi
    pstatus=$(jq -r '.status // empty' "$TMP/parent.json")
    pcurrent=$(jq -r '.run_attempt // empty' "$TMP/parent.json")
    case "$pstatus" in requested|waiting|pending|queued|in_progress|completed) ;; *) echo "::error::unknown parent status '$pstatus'"; exit 1;; esac
    [[ "$pcurrent" =~ ^[1-9][0-9]*$ ]] || { echo "::error::invalid parent attempt '$pcurrent'"; exit 1; }
    if [ "$pstatus" = completed ] || [ "$pcurrent" -gt "$parent_attempt" ]; then
      echo "serial intent $request_id belongs to a terminal/superseded parent attempt; leaving it unprovisioned"
      continue
    fi
    [ "$pcurrent" -eq "$parent_attempt" ] || { echo "::error::parent attempt regressed below intent identity"; exit 1; }
  fi
  parent="standalone"; [ -z "$library_run_id" ] || parent="$library_run_id:$parent_attempt"
  title="relink request=$request_id parent=$parent"
  rows=$(list_runs_all "$REPO" relink.yml)
  matches=$(printf '%s\n' "$rows" | awk -F'\t' -v t="$title" '$3==t')
  count=$(printf '%s\n' "$matches" | awk 'NF' | wc -l | tr -d ' ')
  if [ "$count" -gt 1 ]; then echo "::error::duplicate relink children for intent $request_id"; exit 1; fi
  if [ "$count" -eq 1 ]; then
    status=$(printf '%s\n' "$matches" | cut -f2)
    [ "$status" != completed ] && exit 0
    rid=$(printf '%s\n' "$matches" | cut -f1)
    conclusion=$(gh api "repos/$REPO/actions/runs/$rid" --jq .conclusion)
    [ "$conclusion" = success ] && continue
    echo "::error::intent $request_id has a failed terminal child $rid ($conclusion)"; exit 1
  fi
  active=$(count_runs_active "$REPO" relink.yml)
  [ "$active" -eq 0 ] || { echo "linker busy; intent $request_id remains durable for the next tick"; exit 0; }
  args=(--relink-request-id "$request_id")
  [ -z "$library_run_id" ] || args+=(--library-run-id "$library_run_id")
  [ -z "$parent_attempt" ] || args+=(--parent-run-attempt "$parent_attempt")
  for pair in sefaria_tag:--sefaria-tag snapshot_sha256:--snapshot-sha256 sefaria_release_metadata_sha256:--sefaria-release-metadata-sha256; do
    field=${pair%%:*}; flag=${pair#*:}; value=$(jq -r ".$field" "$TMP/intent/kaggle-intent.json")
    [ -z "$value" ] || args+=("$flag" "$value")
  done
  [ "$(jq -r .dry_run "$TMP/intent/kaggle-intent.json")" = true ] && args+=(--dry-run)
  bash "$DISPATCH_SCRIPT" "${args[@]}"
  exit 0
done
echo "no pending durable Kaggle intent"
