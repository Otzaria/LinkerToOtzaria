# CI wiring (stage 4)

The linker is a stage of the existing weekly pipeline, orchestrated centrally from
`otzaria-library/.github/workflows/weekly-pipeline.yml` (the same place that already
triggers SefariaExport, the library update, and the SeforimLibrary build).

## Flow

```
weekly-pipeline (Thu):
  upstreams  ── SefariaExport release + otzaria-library update (wait for both)
       │
       ├── build   → fire SeforimLibrary manual-generate-release.yml
       │              (build publishes seforim.db.zst + lines_snapshot.db.zst + linker_links → Phase-2)
       └── linker  → fire LinkerToOtzaria relink.yml
                      (self-hosted: Mongo+NER+Django; re-link changed books; publish linker_links.zst)
```

`build` and `linker` run in parallel after `upstreams`. They are **cross-cycle** by design:

- `linker` consumes `lines_snapshot.db.zst` from the **latest** SeforimLibrary release
  (the previous build) and publishes `linker_links.zst`.
- `build`'s Phase-2 (`generateLinkerLinks`, stage 5) consumes the **latest** `linker_links.zst`
  (the previous linker run).

So a newly-changed source book's links land one weekly cycle later. This avoids any
intra-build orchestration and keeps each component independently retriable. The delta
design makes this safe on **both** sides: target-side (Sefaria content) updates are
resolved fresh every build; source-side drift (the offsets index a snapshot that may be up
to two cycles old) is caught by the per-record `source_hash` — Phase-2 safe-drops any link
whose source line no longer matches, and it reappears once that source book is re-linked.

## What each repo contributes

| Repo | Change | Why |
|---|---|---|
| SefariaExport | publishes `changelog_diff.json` (stage 0) | rename hint for target_ref rewrite |
| SeforimLibrary | `dumpLines` step publishes `lines_snapshot.db.zst` (stage 4) | the linker's offset-accurate input |
| SeforimLibrary | `generateLinkerLinks` Phase-2 (stage 5) | resolves target_ref → line, writes clickable links |
| otzaria-library | `weekly-pipeline.yml` `linker` job | triggers the re-link |
| LinkerToOtzaria | `relink.yml` + `ci/*.sh` | the re-link itself |

## Secrets / runner

- `PIPELINE_TOKEN` (PAT) — already used by the pipeline to trigger cross-repo workflows.
- `LINKER_DUMP_URL` — pinned URL of the Sefaria Mongo dump archive (lineage; used by
  `ci/setup_stack.sh`).
- Runner: `[self-hosted, Linux, X64, server-2]` — the heavy stack (Mongo + NER models +
  Django) needs the same class of runner as the DB build, not a hosted `ubuntu-latest`.

## Bootstrap (cold start)

First run has no prior snapshot/zst. Stage 6 seeds them: a full linker run over the whole
corpus populates `artifacts/` + `baseline/` + `meta.json` and publishes the first
`linker_links.zst`; the first SeforimLibrary build with the `dumpLines` step publishes the
first snapshot. After that the steady-state cross-cycle loop holds.
