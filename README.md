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
delta churn. The expensive step (NER) runs only on **source** books that changed.

## Layout

```
artifacts/<source_name>/<canonical_he_title>.jsonl   # one file per source book
baseline/snapshot_hashes.json                        # per-book content hash of the snapshot last linked
meta.json                                            # lineage of the last run
schema/artifact.schema.json                          # JSON Schema for one record
src/linker_artifact.py                               # THE format contract (shared code)
tests/                                               # contract tests
examples/                                            # a validated sample artifact
```

`artifacts/` and `baseline/` are populated by the linker engine (stage 2) and the
incremental driver (stage 3); they start empty.

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
- Ambiguous citations are **dropped** at detection time — artifacts hold only
  unambiguous links.

`src/linker_artifact.py` is the single source of truth for this format and the
`artifacts/<source>/<title>.jsonl` filename scheme. Import it — never re-implement.

## Tests

```
python3 -m unittest discover -s tests -v
```
