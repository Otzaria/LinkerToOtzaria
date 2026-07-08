#!/usr/bin/env bash
# Pack the ref-based artifacts into linker_links.zst and publish it as a release asset the
# SeforimLibrary build downloads. The release also carries meta.json, but that is INFORMATIONAL
# lineage only — source-side correctness is enforced per-record by `source_hash` (Phase-2
# safe-drops any link whose source line no longer matches), NOT by verifying meta.json.
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN required}"
: "${SEFARIA_TAG:?SEFARIA_TAG required}"
REPO="${GITHUB_REPOSITORY:-Otzaria/LinkerToOtzaria}"
TAG="links-${SEFARIA_TAG}"
OUT="linker_links.zst"

# Deterministic tar (sorted, fixed mtimes/owner) so an unchanged corpus packs byte-identically.
tar --sort=name --mtime='UTC 2020-01-01' --owner=0 --group=0 --numeric-owner \
    -cf - artifacts meta.json | zstd -19 -T0 -o "$OUT" -f

SHA="$(sha256sum "$OUT" | cut -d' ' -f1)"
echo "packed $OUT ($(du -h "$OUT" | cut -f1)) sha256=$SHA"

gh release create "$TAG" -R "$REPO" -t "$TAG" -n "Linker links for SefariaExport $SEFARIA_TAG" 2>/dev/null || true
gh release upload "$TAG" -R "$REPO" "$OUT" meta.json --clobber
# Move/keep a stable 'latest' pointer so the build can fetch without knowing the tag.
gh release edit "$TAG" -R "$REPO" --latest
echo "published $TAG"
