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
else
  source_run_id=$GITHUB_RUN_ID
fi

rows=$(gh api --paginate -X GET \
  "repos/$GITHUB_REPOSITORY/actions/runs/$source_run_id/artifacts" -f per_page=100 \
  --jq '.artifacts[] | select((.expired|not) and (.size_in_bytes > 0)) | [.id,.name] | @tsv')
if [ -n "$source_attempt" ]; then
  artifact_name="${prefix}${source_attempt}"
  selected=$(printf '%s\n' "$rows" | awk -F'\t' -v name="$artifact_name" '$2 == name')
else
  selected=$(
    printf '%s\n' "$rows" | awk -F'\t' -v prefix="$prefix" -v current="$GITHUB_RUN_ATTEMPT" '
      index($2,prefix)==1 {
        attempt=substr($2,length(prefix)+1)
        if (attempt ~ /^[1-9][0-9]*$/ && attempt+0 < current+0 && attempt+0 > best) {
          best=attempt+0; row=$0
        }
      }
      END {if (row) print row}
    '
  )
fi
if [ -z "$selected" ]; then
  if [ -n "$source_attempt" ]; then
    echo "::error::explicit checkpoint artifact ${prefix}${source_attempt} is absent or expired" >&2
    exit 1
  fi
  echo "no exact prior-attempt NER checkpoint; starting from zero"
  exit 0
fi
IFS=$'\t' read -r artifact_id artifact_name <<<"$selected"
[[ "$artifact_id" =~ ^[1-9][0-9]*$ ]]
matches=$(printf '%s\n' "$rows" | awk -F'\t' -v name="$artifact_name" '$2 == name {n++} END{print n+0}')
[ "$matches" -eq 1 ] || {
  echo "::error::found $matches live checkpoint artifacts named $artifact_name; refusing to guess" >&2
  exit 1
}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"; [ -z "${source_json:-}" ] || rm -f "$source_json" "$parent_json"' EXIT
gh api "repos/$GITHUB_REPOSITORY/actions/artifacts/$artifact_id/zip" > "$tmp/artifact.zip"
python3 - "$tmp/artifact.zip" "$tmp/files" <<'PY'
import pathlib, sys, zipfile
source=pathlib.Path(sys.argv[1]); target=pathlib.Path(sys.argv[2]); target.mkdir()
with zipfile.ZipFile(source) as archive:
    names=archive.namelist()
    if sorted(names) != ["raw_ner_checkpoint.tar.zst", "raw_ner_checkpoint.tar.zst.sha256"]:
        raise SystemExit(f"unexpected checkpoint artifact members: {names!r}")
    for name in names:
        archive.extract(name, target)
PY
python3 ci/unpack_ner_checkpoint.py \
  "$tmp/files/raw_ner_checkpoint.tar.zst" \
  "$tmp/files/raw_ner_checkpoint.tar.zst.sha256" \
  "$DEST"
echo "restored NER checkpoint artifact $artifact_name"
