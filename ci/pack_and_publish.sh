#!/usr/bin/env bash
# Pack the ref-based artifacts into linker_links.zst. Publishing is deliberately
# performed by the separate ubuntu-hosted publisher job in relink.yml; a GPU/heavy
# compute runner never creates releases or pushes baseline state. meta.json is INFORMATIONAL
# lineage only — source-side correctness is enforced per-record by `source_hash` (Phase-2
# safe-drops any link whose source line no longer matches), NOT by verifying meta.json.
set -euo pipefail

OUT="linker_links.zst"
test -f line-baseline/manifest.json

# shellcheck source=ci/zstd_mt.sh
. "$(dirname "$0")/zstd_mt.sh"
WORKERS="$(zstd_workers)"

# Packing tuning, measured on the 667 MiB / 13808-file linker corpus of release
# linker-release-sha256-e7133000...  (WSL2 Ubuntu 24.04, 16 vCPU, zstd 1.5.5):
#
#   -19 -T0                 81-92 s   714% CPU   131,374,640 B   (previous)
#   -19 -T16                75-78 s  1068% CPU   131,374,640 B   (byte-identical)
#   -19 -T16 -B4M ovlog=6   47-53 s  1485% CPU   128,692,034 B   (this; two independent runs)
#
# Two independent limiters, both fixed here:
#   1. `-T0` resolves to PHYSICAL cores (8 of 16 here) -- see ci/zstd_mt.sh.
#   2. zstd's default job size at level 19 is 32 MiB, so a 678 MiB tar is only
#      21 jobs.  With 16 workers that is one full wave plus a 5-job tail, which
#      caps utilisation near 66% no matter how many workers exist.  `-B4M` cuts
#      the jobs to ~170 so every worker stays fed to the end.
#      Smaller jobs would normally cost ratio, but zstd's DEFAULT overlap at
#      level 19 is a FULL window (ovlog=9 => 8 MiB), so every job re-processes
#      8 MiB of prefix.  That is the bulk of the CPU cost and it does not even
#      buy ratio: measured, ovlog=9 is both ~2x more CPU and ~2.2 MB LARGER than
#      ovlog=6 (1 MiB overlap) on this corpus.  Hence -B4M + ovlog=6 is a strict
#      win on both axes: 2.68 MB SMALLER (-2.04%) and roughly half the wall time.
# Neither -B nor ovlog changes the container: the result is still a plain zstd
# frame over the same tar, decoded by an unmodified `zstd -dc` with no flags.
# Both are pinned explicitly, so the output no longer depends on zstd's defaults
# for job size and overlap (the level-19 cParams still come from the zstd build).
ZSTD_TUNING=(-19 -B4M --zstd=ovlog=6)

# Fail loudly rather than silently falling back: a silent fallback would make
# the payload bytes depend on the local zstd build, breaking content addressing.
if ! printf '' | zstd "${ZSTD_TUNING[@]}" -T"$WORKERS" -q -o /dev/null -f 2>/dev/null; then
  echo "::error::zstd does not accept ${ZSTD_TUNING[*]}; refusing to pack with different settings" >&2
  zstd --version >&2 || true
  exit 1
fi

# Deterministic tar (sorted, fixed mtimes/owner) so an unchanged corpus packs byte-identically.
tar --sort=name --mtime='UTC 2020-01-01' --owner=0 --group=0 --numeric-owner \
    --exclude='*/.DS_Store' --exclude='*/.gitkeep' --exclude='*/._*' \
    -cf - artifacts line-baseline meta.json \
  | zstd "${ZSTD_TUNING[@]}" -T"$WORKERS" -o "$OUT" -f

SHA="$(sha256sum "$OUT" | cut -d' ' -f1)"
echo "packed $OUT ($(du -h "$OUT" | cut -f1)) sha256=$SHA"

printf '%s\n' "$SHA" > linker_links.zst.sha256
