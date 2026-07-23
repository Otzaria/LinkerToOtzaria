#!/usr/bin/env bash
# Restore the newest exact prior-attempt checkpoint from this workflow databaseId.
set -euo pipefail

[ "$#" -eq 1 ] || { echo "usage: $0 DESTINATION" >&2; exit 2; }
DEST=$1
[[ "${RELINK_REQUEST_ID:-}" =~ ^[0-9a-f]{64}$ ]]
[[ "${GITHUB_RUN_ID:-}" =~ ^[1-9][0-9]*$ ]]
[[ "${GITHUB_RUN_ATTEMPT:-}" =~ ^[1-9][0-9]*$ ]]
[[ -n "${GITHUB_REPOSITORY:-}" ]]

prefix="raw-ner-checkpoint-${RELINK_REQUEST_ID}-"
rows=$(gh api --paginate -X GET \
  "repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID/artifacts" -f per_page=100 \
  --jq '.artifacts[] | select((.expired|not) and (.size_in_bytes > 0)) | [.id,.name] | @tsv')
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
if [ -z "$selected" ]; then
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
trap 'rm -rf "$tmp"' EXIT
gh api -H 'Accept: application/octet-stream' \
  "repos/$GITHUB_REPOSITORY/actions/artifacts/$artifact_id/zip" > "$tmp/artifact.zip"
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
