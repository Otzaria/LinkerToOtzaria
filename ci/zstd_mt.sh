#!/usr/bin/env bash
# Shared multi-threading policy for every deterministic `producer | zstd` pack step.
#
# WHY THIS EXISTS
# ---------------
# `zstd -T0` does NOT mean "use every CPU".  It resolves to the number of
# PHYSICAL cores (zstd's UTIL_countPhysicalCores(), which de-duplicates
# /proc/cpuinfo by "core id"), not the number of logical CPUs the scheduler
# will actually give us.  On the self-hosted runner (Ubuntu in WSL2, 16 vCPUs
# exposed as 8 physical + 8 sibling threads) `-T0` therefore starts only 8
# workers.  Measured on the 667 MiB linker corpus, `-19 -T0` ran at 714% CPU
# for 92 s on a box with 1600% available -- and ~29% of the 28-thread Windows
# host, which is exactly the "~30% CPU" the owner observed.
#
# The tar/python producer is NOT the bottleneck: the same tar streamed to `cat`
# completes in 0.42 s warm (678 MiB), i.e. ~200x faster than the compressor.
#
# DETERMINISM
# -----------
# zstd's frame output depends on the compression level, the job size and the
# overlap size -- NOT on how many workers chew through those jobs.  Pinning the
# worker count to the logical CPU count is therefore byte-neutral: verified
# empirically at -T2/-T4/-T8(-T0)/-T16, all identical (see PACKING_PERF notes in
# the review). Because output is worker-count independent, this is safe to vary
# per machine.
zstd_workers() {
  local n
  n="$(nproc 2>/dev/null || echo 0)"
  case "$n" in
    ''|*[!0-9]*) n=0 ;;
  esac
  # Bound worst-case resident set: each worker holds roughly one job buffer plus
  # one match-finder context.  32 workers is ~3 GB at level 19, which is well
  # inside the runner's budget while still saturating any realistic runner.
  if [ "$n" -gt 32 ]; then
    n=32
  fi
  # 0 falls back to zstd's own detection, i.e. exactly the previous behaviour.
  printf '%s\n' "$n"
}
