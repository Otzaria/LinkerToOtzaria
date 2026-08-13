#!/usr/bin/env bash
# Restore an exact resumable checkpoint. Normal reruns use the newest prior attempt
# of this databaseId; an explicit recovery may name one failed source run/attempt.
set -euo pipefail

[ "$#" -eq 1 ] || { echo "usage: $0 DESTINATION" >&2; exit 2; }
DEST=$1
[[ "${RELINK_REQUEST_ID:-}" =~ ^[0-9a-f]{64}$ ]]
[[ "${GITHUB_RUN_ID:-}" =~ ^[1-9][0-9]*$ ]]
[[ "${GITHUB_RUN_ATTEMPT:-}" =~ ^[1-9][0-9]*$ ]]
[[ -n "${GITHUB_REPOSITORY:-}" ]]

prefix="raw-ner-checkpoint-${RELINK_REQUEST_ID}-"
source_run_id=${NER_CHECKPOINT_SOURCE_RUN_ID:-}
source_attempt=${NER_CHECKPOINT_SOURCE_RUN_ATTEMPT:-}
if [ -n "$source_run_id" ] || [ -n "$source_attempt" ]; then
  [[ "$source_run_id" =~ ^[1-9][0-9]*$ && "$source_attempt" =~ ^[1-9][0-9]*$ ]]
  [[ "${LIBRARY_RUN_ID:-}" =~ ^[1-9][0-9]*$ ]]
  [[ "${PARENT_RUN_ATTEMPT:-}" =~ ^[1-9][0-9]*$ ]]
  expected_title="relink request=${RELINK_REQUEST_ID} parent=${LIBRARY_RUN_ID}:${PARENT_RUN_ATTEMPT}"
  expected_recovery_title="relink-recovery request=${RELINK_REQUEST_ID} parent=${LIBRARY_RUN_ID}:${PARENT_RUN_ATTEMPT}"
  source_json=$(mktemp)
  parent_json=$(mktemp)
  trap 'rm -f "$source_json" "$parent_json"' EXIT
  gh api \
    "repos/$GITHUB_REPOSITORY/actions/runs/$source_run_id/attempts/$source_attempt" \
    > "$source_json"
  jq -e \
    --arg title "$expected_title" \
    --arg recovery_title "$expected_recovery_title" \
    --argjson attempt "$source_attempt" '
      .conclusion as $conclusion |
      .status == "completed" and
      (["failure","cancelled","timed_out","action_required","startup_failure","stale"]
       | index($conclusion)) != null and
      .event == "workflow_dispatch" and
      .path == ".github/workflows/relink.yml" and
      (.display_title == $title or .display_title == $recovery_title) and
      .run_attempt == $attempt
    ' "$source_json" >/dev/null || {
      echo "::error::checkpoint source is not the exact failed producer attempt" >&2
      exit 1
    }
  gh api \
    "repos/Otzaria/SeforimLibrary/actions/runs/$LIBRARY_RUN_ID/attempts/$PARENT_RUN_ATTEMPT" \
    > "$parent_json"
  jq -e --argjson attempt "$PARENT_RUN_ATTEMPT" '
      .conclusion as $conclusion |
      .status == "completed" and
      (["failure","cancelled","timed_out","action_required","startup_failure","stale"]
       | index($conclusion)) != null and
      .event == "workflow_dispatch" and
      .path == ".github/workflows/manual-generate-release.yml" and
      .run_attempt == $attempt
    ' "$parent_json" >/dev/null || {
      echo "::error::checkpoint recovery parent is not the exact failed build attempt" >&2
      exit 1
    }
fi

if [ -z "$source_attempt" ]; then
  source_attempt=$(gh api --paginate -X GET "repos/$GITHUB_REPOSITORY/releases" -f per_page=100 \
    --jq '.[].tag_name' | awk -v prefix="$prefix" -v current="$GITHUB_RUN_ATTEMPT" '
      index($0,prefix)==1 {
        attempt=substr($0,length(prefix)+1)
        if (attempt ~ /^[1-9][0-9]*$/ && attempt+0 < current+0 && attempt+0 > best) best=attempt+0
      }
      END {if (best) print best}
    ')
fi
if [ -z "$source_attempt" ]; then
  echo "no exact prior-attempt NER checkpoint; starting from zero"
  exit 0
fi
release_tag="${prefix}${source_attempt}"
source_meta=$(mktemp)
gh api "repos/$GITHUB_REPOSITORY/actions/runs/${source_run_id:-$GITHUB_RUN_ID}/attempts/$source_attempt" > "$source_meta"
source_head=$(jq -er .head_sha "$source_meta")
gh release view "$release_tag" --json isDraft,isPrerelease,targetCommitish,assets > "$source_meta.release"
python3 - "$source_meta.release" "$source_head" <<'PY'
import json,sys
value=json.load(open(sys.argv[1],encoding='utf-8'))
names={item.get('name') for item in value.get('assets',[])}
if not (value.get('isDraft') is False and value.get('isPrerelease') is True and
        value.get('targetCommitish')==sys.argv[2] and
        names=={'raw_ner_checkpoint.tar.zst','raw_ner_checkpoint.tar.zst.sha256'}):
 raise SystemExit('checkpoint release identity/assets differ')
PY
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"; rm -f "$source_meta" "$source_meta.release"; [ -z "${source_json:-}" ] || rm -f "$source_json" "$parent_json"' EXIT
mkdir "$tmp/files"
gh release download "$release_tag" -p raw_ner_checkpoint.tar.zst -p raw_ner_checkpoint.tar.zst.sha256 -D "$tmp/files"
python3 ci/unpack_ner_checkpoint.py \
  "$tmp/files/raw_ner_checkpoint.tar.zst" \
  "$tmp/files/raw_ner_checkpoint.tar.zst.sha256" \
  "$DEST"
echo "restored NER checkpoint release $release_tag"
