# LinkerToOtzaria

Dedicated store and pipeline for **citation links** detected by the Sefaria linker,
kept as stable, delta-friendly, ref-based artifacts that the SeforimLibrary build
turns into clickable in-app links.

Full design: `SeforimLibrary/LINKER_DELTA_PLAN.md` and
`SeforimLibrary/LINKER_IMPLEMENTATION_STAGES.md`.

## Why ref-based (the core idea)

A link has two ends:
- **source** — a span in a book (line + char range of the citation phrase),
- **target** — a Sefaria **ref** (`"Yoma 55a:5"`), a stable logical id.

We store the **ref**, not a resolved target line. The build resolves it to a line
every time (via `resolveRefs`). Because Sefaria line ids are keyed on `REF:$heRef`,
a Sefaria content update keeps the same line id → the link survives with **zero**
delta churn. The expensive step (NER) runs only on **source lines** that are new or
changed. An exact release-only line baseline reuses prior artifacts for identical
lines, including lines that moved inside an edited book.

## Layout

```
artifacts/<source_name>/<canonical_he_title>.jsonl   # one file per source book
baseline/snapshot_hashes.json                        # per-book content hash of the snapshot last linked
line-baseline/                                       # release-only exact line fingerprints + artifact digests
meta.json                                            # lineage of the last run
schema/artifact.schema.json                          # JSON Schema for one record
src/linker_artifact.py                               # THE format contract (shared code)
tests/                                               # contract tests
examples/                                            # a validated sample artifact
```

`artifacts/` and `line-baseline/` are generated release payload state and are not
committed. `baseline/` and `meta.json` are committed only after the immutable
payload is published.

## Record format

One JSON object per line (see `schema/artifact.schema.json`):

```json
{"book_key": {"source_name": "MoreBooks", "canonical_he_title": "חזון איש"},
 "line_index": 28, "line_index_base": 0,
 "start": 37, "end": 48,
 "target_ref": "Psalms 16:8"}
```

- `book_key` — the source book identity, **identical** to the generator's
  `BookKey(sourceName, canonicalHeTitle)`. Derived from the DB as
  `(source.name, COALESCE(book.heRef, book.title))`. Collision-free (verified across
  the full corpus).
- `line_index` — 0-based, equals `line.lineIndex` in the DB.
- `start` / `end` — offsets of the citation span into the **stored** line content
  (`line.content`, HTML included). The build maps raw→visible via `countVisibleChars`.
- `target_ref` — canonical English ref (`Ref.normal()`). The only thing the build
  needs to resolve the target.
- The snapshot also carries a per-line `context_ref`: exact `line.heRef` when
  available, otherwise the canonical book title. It is passed to Sefaria as
  `book_context_refs`, enabling CURRENT_BOOK resolution for relative citations
  such as `לעיל` and `לקמן`. Resolver-context changes participate in both the
  book change clock and exact-line reuse fingerprint.
- Structural `<h1>`…`<h6>` lines are never sent to NER and never emitted as
  artifacts. The Phase-2 DB importer independently rejects heading records from
  old/external artifacts.
- Ambiguous citations are **dropped** at detection time — artifacts hold only
  unambiguous links.

`src/linker_artifact.py` is the single source of truth for this format and the
`artifacts/<source>/<title>.jsonl` filename scheme. Import it — never re-implement.

## Tests

```
python3 -m unittest discover -s tests -v
```

## Parallel full-corpus relink

The expensive NER pass can be split by whole books and run independently from
the CPU resolver:

```bash
python3 src/parallel_ner.py split \
  --snapshot lines.db --plan full-plan.json --output shards --count 12

python3 scripts/parallel_kaggle_ner.py dispatch \
  --state kaggle-state --dataset OWNER/PRIVATE_INPUT_DATASET \
  --prefix linker-ner-RUN_ID --count 10 \
  --session-budget-seconds 9700 --reserve-hours 2

scripts/run_cpu_ner_shard.sh INPUT WORK OUTPUT 10
scripts/run_cpu_ner_shard.sh INPUT WORK OUTPUT 11

python3 src/parallel_ner.py merge \
  --snapshot lines.db --plan full-plan.json \
  --bundle raw-ner-shard-00 --bundle raw-ner-shard-01 \
  --bundle raw-ner-shard-02 --bundle raw-ner-shard-03 \
  --bundle raw-ner-shard-04 --bundle raw-ner-shard-05 \
  --bundle raw-ner-shard-06 --bundle raw-ner-shard-07 \
  --bundle raw-ner-shard-08 --bundle raw-ner-shard-09 \
  --bundle raw-ner-shard-10 --bundle raw-ner-shard-11 \
  --output merged-ner --expected-batch-lines 75
```

`parallel_ner.py merge` rejects missing, duplicate, overlapping, or
contract-mismatched books. Replay the merged bundle through `incremental.py`
with `--ner-bundle-dir merged-ner --accumulate-existing`. Accumulation retains
only still-valid prior records (same line content, valid UTF-16 span, and a
non-heading source line), unions new records by semantic identity, and therefore
adds newly detected links without preserving stale or heading links.

Kaggle workers run only `precompute_ner.py`; MongoDB and reference resolution
stay on CPU. `parallel_kaggle_ner.py` checks the live quota before dispatch and
refuses a launch whose worst-case session budgets would violate the requested
GPU-hour reserve. The private input manifest pins and hashes the snapshot,
plans, models, runtime, and source archives so a branch can repeat the run.
