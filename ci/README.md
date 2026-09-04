# CI wiring (stage 4)

The linker is a stage of the existing weekly pipeline, orchestrated centrally from
`otzaria-library/.github/workflows/weekly-pipeline.yml` (the same place that already
triggers SefariaExport, the library update, and the SeforimLibrary build).

## Flow

```
weekly-pipeline (Thu):
  upstreams → SeforimLibrary manual-generate-release.yml
                │ build DB + publish exact content-addressed lines_snapshot release
                │ release shared host lease
                ▼
           kaggle-relink intent/provisioner
                │ relink job on Kaggle: plan + GPU NER only (≤60m producer)
                │ content-addressed raw-NER artifact
                ▼
           resolve job on Oracle ARM
                │ bounded wait for shared host lease
                │ Mongo + Django CPU resolution (NER models disabled)
                │ immutable payload + manifest
                ▼
           publisher → waiting build verifies exact identity and applies links
```

The serial build waits for its exact correlated child. There is no cross-cycle “Latest”
handoff: request id, parent run+attempt, snapshot digest, pinned Sefaria metadata,
engine fingerprint and payload digest are checked end-to-end before Phase-2 mutates the DB.
The parent releases the kernel host lease for both targets because the split Kaggle path
also needs the Oracle box for CPU resolution, and reacquires it only after the child is
terminal.

## What each repo contributes

| Repo | Change | Why |
|---|---|---|
| SefariaExport | publishes `changelog_diff.json` (stage 0) | rename hint for target_ref rewrite |
| SeforimLibrary | `dumpLines` step publishes `lines_snapshot.db.zst` on a content-addressed pre-release (stage 4) | the linker's offset-accurate input |
| SeforimLibrary | DB release's `build_provenance.json` records `snapshot_release_tag` / `snapshot_zst_sha256` | lets a manual relink resolve and verify that pre-release, so the DB release need not carry a duplicate ~1 GiB copy |
| SeforimLibrary | `generateLinkerLinks` Phase-2 (stage 5) | resolves target_ref → line, writes clickable links |
| otzaria-library | `weekly-pipeline.yml` | orchestrates the single serial saga |
| LinkerToOtzaria | `relink.yml` + `ci/*.sh` | GPU NER handoff, CPU resolution, immutable publish |

## Secrets / runner

- `PIPELINE_TOKEN` (PAT) — already used by the pipeline to trigger cross-repo workflows.
- `LINKER_DUMP_URL` — pinned URL of the Sefaria Mongo dump archive (lineage; used by
  `ci/setup_stack.sh`).
- Kaggle JIT runner: GPU NER only; no Mongo restore and no `sefaria.model` import.
- Oracle runner: `[self-hosted, Linux, ARM64, server-2]`; Mongo+Django resolution under
  `/run/lock/otzaria/host-heavy.lock`, without GPU/NER models in memory.

## Bootstrap (cold start)

First run has no prior snapshot/zst. Stage 6 seeds them: a full linker run over the whole
corpus populates `artifacts/` + `baseline/` + `meta.json` and publishes the first
`linker_links.zst`; the first SeforimLibrary build with `dumpLines` publishes the first
snapshot. After that every build uses the same-cycle serial contract above.

## Recovery

- NER exceeded its 60-minute producer budget: rerun failed jobs on the same relink
  databaseId; the next attempt restores the newest unique prior checkpoint.
- Resolver failed after raw NER upload: dispatch `relink.yml` in resolver-only recovery
  mode using the exact source run+attempt. This skips Kaggle and revalidates the source
  run, parent, commit, artifact, snapshot and engine fingerprint.
- A busy Oracle host is not an immediate loss of paid GPU work: resolver waits up to
  60 minutes on the real kernel lease, then fails loudly for bounded recovery.
