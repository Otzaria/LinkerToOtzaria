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
  # The DB release stopped carrying a second copy of lines_snapshot.db.zst (build
  # provenance schema_version 5 on) — the bytes only ever lived on the
  # content-addressed pre-release the build published before its own relink, and
  # the DB release now NAMES that pre-release. Resolve it from
  # build_provenance.json and verify the snapshot against the digest recorded
  # there: the same fail-closed check serial mode makes above, and strictly
  # stronger than the old self-consistent descriptor compare.
  rm -f inputs/build_provenance.json
  gh release download "$LIB_TAG" -R Otzaria/SeforimLibrary \
    -p build_provenance.json -D inputs --clobber
  PROV_SNAPSHOT_SHA="$(jq -r '.snapshot_zst_sha256 // ""' inputs/build_provenance.json)"
  PROV_SNAPSHOT_TAG="$(jq -r '.snapshot_release_tag // ""' inputs/build_provenance.json)"
  if [[ "$PROV_SNAPSHOT_SHA" =~ ^[0-9a-f]{64}$ ]]; then
    [[ "$PROV_SNAPSHOT_TAG" == "lines-snapshot-sha256-$PROV_SNAPSHOT_SHA" ]] || {
      echo "::error::$LIB_TAG build_provenance.json snapshot tag does not match its snapshot digest" >&2; exit 1;
    }
    gh release download "$PROV_SNAPSHOT_TAG" -R Otzaria/SeforimLibrary \
      -p lines_snapshot.db.zst -D inputs --clobber
    REMOTE_DIGEST="$(gh release view "$PROV_SNAPSHOT_TAG" -R Otzaria/SeforimLibrary --json assets \
      --jq '.assets[]|select(.name=="lines_snapshot.db.zst")|.digest')"
    [[ "$REMOTE_DIGEST" == "sha256:$PROV_SNAPSHOT_SHA" ]]
    echo "$PROV_SNAPSHOT_SHA  inputs/lines_snapshot.db.zst" | sha256sum -c -
  else
    # LEGACY: DB releases up to and including v26 shipped the snapshot themselves
    # and their provenance (schema_version <= 4) names no pre-release. Keeps manual
    # mode working until the latest DB release carries snapshot_release_tag, i.e.
    # from v27 on — delete this branch then.
    gh release download "$LIB_TAG" -R Otzaria/SeforimLibrary \
      -p lines_snapshot.db.zst -D inputs --clobber
    REMOTE_DIGEST="$(gh release view "$LIB_TAG" -R Otzaria/SeforimLibrary --json assets \
      --jq '.assets[]|select(.name=="lines_snapshot.db.zst")|.digest')"
    [[ "$REMOTE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
    [[ "$(sha256sum inputs/lines_snapshot.db.zst | cut -d' ' -f1)" == "${REMOTE_DIGEST#sha256:}" ]]
  fi
fi
ACTUAL_SNAPSHOT_SHA="$(sha256sum inputs/lines_snapshot.db.zst | cut -d' ' -f1)"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "snapshot_zst_sha256=$ACTUAL_SNAPSHOT_SHA" >> "$GITHUB_OUTPUT"
fi
unzstd -f inputs/lines_snapshot.db.zst
