#!/usr/bin/env bash
# Pack the ref-based artifacts into linker_links.zst. Publishing is deliberately
# performed by the separate ubuntu-hosted publisher job in relink.yml; a GPU/heavy
# compute runner never creates releases or pushes baseline state. meta.json is INFORMATIONAL
# lineage only — source-side correctness is enforced per-record by `source_hash` (Phase-2
# safe-drops any link whose source line no longer matches), NOT by verifying meta.json.
set -euo pipefail

OUT="linker_links.zst"
test -f line-baseline/manifest.json

# Deterministic tar (sorted, fixed mtimes/owner) so an unchanged corpus packs byte-identically.
tar --sort=name --mtime='UTC 2020-01-01' --owner=0 --group=0 --numeric-owner \
    --exclude='*/.DS_Store' --exclude='*/.gitkeep' --exclude='*/._*' \
    -cf - artifacts line-baseline meta.json | zstd -19 -T0 -o "$OUT" -f

SHA="$(sha256sum "$OUT" | cut -d' ' -f1)"
echo "packed $OUT ($(du -h "$OUT" | cut -f1)) sha256=$SHA"

printf '%s\n' "$SHA" > linker_links.zst.sha256
