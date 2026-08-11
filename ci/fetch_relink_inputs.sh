#!/usr/bin/env bash
# Fetch and content-verify the exact snapshot/changelog pair for either compute stage.
set -euo pipefail

: "${SEF_TAG:?SEF_TAG is required}"
mkdir -p inputs
gh release download "$SEF_TAG" -R Otzaria/SefariaExport \
  -p changelog_diff.json -D inputs --clobber
gh release download "$SEF_TAG" -R Otzaria/SefariaExport \
  -p release_metadata.json -D inputs --clobber
if [ -n "${LIBRARY_RUN_ID:-}" ]; then
  [[ "${SEFARIA_METADATA_SHA256:-}" =~ ^[0-9a-f]{64}$ ]]
  echo "$SEFARIA_METADATA_SHA256  inputs/release_metadata.json" | sha256sum -c -
fi
CL_SHA="$(jq -r '.changelog.sha256' inputs/release_metadata.json)"
[[ "$CL_SHA" =~ ^[0-9a-f]{64}$ ]]
echo "$CL_SHA  inputs/changelog_diff.json" | sha256sum -c -
rm -f inputs/lines_snapshot.db.zst inputs/lines_snapshot.db
if [ -n "${LIBRARY_RUN_ID:-}" ]; then
  [[ "$LIBRARY_RUN_ID" =~ ^[1-9][0-9]*$ ]]
  [[ "${PARENT_RUN_ATTEMPT:-}" =~ ^[1-9][0-9]*$ ]]
  [[ "${SNAPSHOT_SHA256:-}" =~ ^[0-9a-f]{64}$ ]]
  SNAPSHOT_TAG="lines-snapshot-sha256-$SNAPSHOT_SHA256"
  gh release download "$SNAPSHOT_TAG" -R Otzaria/SeforimLibrary \
    -p lines_snapshot.db.zst -D inputs --clobber
  REMOTE_DIGEST="$(gh release view "$SNAPSHOT_TAG" -R Otzaria/SeforimLibrary --json assets \
    --jq '.assets[]|select(.name=="lines_snapshot.db.zst")|.digest')"
  [[ "$REMOTE_DIGEST" == "sha256:$SNAPSHOT_SHA256" ]]
  echo "$SNAPSHOT_SHA256  inputs/lines_snapshot.db.zst" | sha256sum -c -
else
  LIB_TAG="$(gh release view -R Otzaria/SeforimLibrary --json tagName -q .tagName)"
  [[ "$LIB_TAG" =~ ^[A-Za-z0-9._-]{1,150}$ ]]
  gh release download "$LIB_TAG" -R Otzaria/SeforimLibrary \
    -p lines_snapshot.db.zst -D inputs --clobber
  REMOTE_DIGEST="$(gh release view "$LIB_TAG" -R Otzaria/SeforimLibrary --json assets \
    --jq '.assets[]|select(.name=="lines_snapshot.db.zst")|.digest')"
  [[ "$REMOTE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
  [[ "$(sha256sum inputs/lines_snapshot.db.zst | cut -d' ' -f1)" == "${REMOTE_DIGEST#sha256:}" ]]
fi
ACTUAL_SNAPSHOT_SHA="$(sha256sum inputs/lines_snapshot.db.zst | cut -d' ' -f1)"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "snapshot_zst_sha256=$ACTUAL_SNAPSHOT_SHA" >> "$GITHUB_OUTPUT"
fi
unzstd -f inputs/lines_snapshot.db.zst
