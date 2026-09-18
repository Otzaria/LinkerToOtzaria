"""Incremental driver (stage 3): re-link only what changed, keep links stable.

Source of truth for WHICH SOURCE BOOKS CHANGED = the `lines_snapshot.db` the linker is
actually handed, NOT the upstream manifests. This is the crux: the linker can only ever
link snapshot content and stamps each record's `source_hash` from it, so change-detection
and the baseline MUST track the *snapshot*, or the two clocks drift (a book linked against
a stale snapshot gets stamped with content that never matches the build's DB, its links are
safe-dropped by Phase-2, and — if the baseline advanced by some *other* clock — it would
never be re-linked). Keying everything off the snapshot makes the whole loop coherent by
construction: link snapshot content, stamp snapshot hashes, advance the baseline to exactly
those snapshot hashes.

  • changed  = books whose per-book content hash in the CURRENT snapshot differs from the
               baseline (includes books new to the snapshot) → re-link.
  • removed  = books in the baseline no longer in the snapshot → delete their artifacts.
  • baseline advances to the current snapshot hashes ONLY after a fully successful engine
    run. ANY per-book crash (the run's `failed/` ledger) fails the whole run loudly — in
    the serial pipeline the build is waiting on this output, and a missing book would ship
    a silently-incomplete DB. Rerun retries everything (baseline untouched).
  • the baseline also records an ENGINE FINGERPRINT (Sefaria/gpu-server commits, model
    versions, policy flags — assembled by the workflow). A fingerprint change invalidates
    the whole baseline → full relink, so the artifact store is never a mix of engines.

The changelog (`changelog_diff.json`) is used ONLY to rewrite `target_ref` for Sefaria
English renames — never for source-change detection. That is best-effort (latest changelog
only); a rename missed in a skipped cycle is SAFE, not silent-wrong: the stale ref fails
`resolveRefs` at build time and the link is dropped (never mis-pointed), regenerated when
that source book is next re-linked. `titles.json`/manifests are no longer needed here —
source identity (source_name, canonical_he_title) is read straight from the snapshot.

This module keeps the pure logic (hash/diff/rewrite/relocate/meta) importable and
side-effect-free; main() does the I/O and external calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3

sys_path = os.path.dirname(os.path.abspath(__file__))
import sys  # noqa: E402
sys.path.insert(0, sys_path)
from linker_artifact import (  # noqa: E402
    BookKey,
    book_key_to_relpath,
    content_hash,
    read_artifact,
    remove_artifact,
    write_artifact,
)
from line_baseline import (  # noqa: E402
    DIRECTORY_NAME as LINE_BASELINE_DIRECTORY,
    build_line_baseline,
    plan_changed_books,
    validate_baseline_identity,
)
from ner_handoff import SCHEMA_VERSION  # noqa: E402


# ── target_ref rewrite for en-renames (no linking) ──────────────────────────

def rewrite_target_ref(ref: str, old_en: str, new_en: str) -> str:
    """If `ref` targets book `old_en`, return it retitled to `new_en`; else unchanged.

    A ref is `<Title> <sections>` and sections always start with a digit, while a
    longer title's extra word starts with a letter. So we rewrite only when the ref
    equals old_en or continues with a space+digit — this keeps "Genesis" from
    matching "Genesis Rabbah 1:1" (the classic prefix trap)."""
    if ref == old_en:
        return new_en
    prefix = old_en + " "
    if ref.startswith(prefix):
        rest = ref[len(prefix):]
        if rest[:1].isdigit():
            return new_en + " " + rest
    return ref


def apply_en_renames(artifacts_dir: str, en_renames: list[dict]) -> int:
    """Rewrite target_ref across all artifacts per en_renamed pairs. Returns records changed."""
    pairs = [(r["old_en"], r["new_en"]) for r in en_renames if r.get("old_en") and r.get("new_en")]
    if not pairs:
        return 0
    changed = 0
    for path in _iter_artifacts(artifacts_dir):
        recs = list(read_artifact(path))
        new_recs = []
        touched = False
        for rec in recs:
            tr = rec.target_ref
            for old_en, new_en in pairs:
                tr = rewrite_target_ref(tr, old_en, new_en)
            if tr != rec.target_ref:
                touched = True
                changed += 1
                rec = _with_target_ref(rec, tr)
            new_recs.append(rec)
        if touched:
            write_artifact(path, new_recs)
    return changed


def _with_target_ref(rec, new_ref):
    from linker_artifact import LinkRecord
    return LinkRecord(
        book_key=rec.book_key, line_index=rec.line_index, start=rec.start,
        end=rec.end, target_ref=new_ref, line_index_base=rec.line_index_base,
        source_path=rec.source_path, source_hash=rec.source_hash,
        context_ref=rec.context_ref, relative_direction=rec.relative_direction,
    )


def _iter_artifacts(artifacts_dir: str):
    for root, _dirs, files in os.walk(artifacts_dir):
        for f in files:
            if f.endswith(".jsonl"):
                yield os.path.join(root, f)


# ── source-book artifact relocation (rename / delete) ────────────────────────

def relocate_source_artifact(repo: str, old_key: BookKey, new_key: BookKey) -> bool:
    """Move a source book's artifact to its new book_key and rewrite the embedded
    book_key in every record. Returns True if a move happened."""
    old_path = os.path.join(repo, book_key_to_relpath(old_key))
    if not os.path.exists(old_path):
        return False
    recs = [_with_book_key(r, new_key) for r in read_artifact(old_path)]
    new_path = os.path.join(repo, book_key_to_relpath(new_key))
    write_artifact(new_path, recs)
    if os.path.abspath(new_path) != os.path.abspath(old_path):
        remove_artifact(old_path)
    return True


def delete_source_artifact(repo: str, key: BookKey) -> bool:
    return remove_artifact(os.path.join(repo, book_key_to_relpath(key)))


def _with_book_key(rec, bk):
    from linker_artifact import LinkRecord
    return LinkRecord(
        book_key=bk, line_index=rec.line_index, start=rec.start, end=rec.end,
        target_ref=rec.target_ref, line_index_base=rec.line_index_base,
        source_path=rec.source_path, source_hash=rec.source_hash,
        context_ref=rec.context_ref, relative_direction=rec.relative_direction,
    )


# ── snapshot content hashing: the ONLY source-change clock ───────────────────

def snapshot_book_hashes(snapshot_db: str) -> dict[tuple[str, str], str]:
    """Per-book content hash over the snapshot's lines, keyed by (source_name, he_title).

    Streams rows ordered by book then line_index and folds each book's
    (line_index, content, context_ref) into one sha1 — so any content, location
    context or line change flips the hash, and it never materialises a
    whole book in memory. This is the exact content the linker links and stamps `source_hash`
    from, so a hash change here is precisely "this book must be re-linked"."""
    con = sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT source_name, canonical_he_title, line_index, content, context_ref "
            "FROM lines_snapshot ORDER BY source_name, canonical_he_title, line_index"
        )
        hashes: dict[tuple[str, str], str] = {}
        cur_key = None
        h = None
        for s, t, li, content, context_ref in rows:
            key = (s, t)
            if key != cur_key:
                if cur_key is not None:
                    hashes[cur_key] = h.hexdigest()[:16]
                cur_key = key
                h = hashlib.sha1()
            h.update(str(li).encode("ascii"))
            h.update(b"\0")
            h.update((content or "").encode("utf-8"))
            h.update(b"\0")
            h.update((context_ref or "").encode("utf-8"))
            h.update(b"\0")
        if cur_key is not None:
            hashes[cur_key] = h.hexdigest()[:16]
        return hashes
    finally:
        con.close()


def plan_from_snapshot(
    current: dict[tuple[str, str], str], baseline: dict[tuple[str, str], str]
) -> tuple[list[BookKey], list[BookKey]]:
    """(changed, removed) book lists from a snapshot-hash diff.

    changed = new or content-changed vs baseline → re-link.
    removed = in baseline but gone from the snapshot → delete artifact."""
    changed = [BookKey(s, t) for (s, t) in sorted(current) if current[(s, t)] != baseline.get((s, t))]
    removed = [BookKey(s, t) for (s, t) in sorted(baseline) if (s, t) not in current]
    return changed, removed


# ── baseline + lineage ───────────────────────────────────────────────────────

_BASELINE_NAME = "snapshot_hashes.json"


def read_failed_books(run_dir: str) -> set[tuple[str, str]]:
    """book_keys the engine marked failed (a per-book crash inside link_books.py). Each is a
    file under <run-dir>/failed whose content is the book_key. The driver holds these OUT of
    the baseline so they stay 'changed' and are retried next cycle — never silently orphaned."""
    failed_dir = os.path.join(run_dir, "failed")
    out: set[tuple[str, str]] = set()
    if not os.path.isdir(failed_dir):
        return out
    for name in os.listdir(failed_dir):
        p = os.path.join(failed_dir, name)
        if not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        out.add((d["source_name"], d["canonical_he_title"]))
    return out


def read_snapshot_baseline(baseline_dir: str) -> dict[tuple[str, str], str]:
    return _read_baseline_file(baseline_dir)[0]


def read_baseline_fingerprint(baseline_dir: str) -> str | None:
    return _read_baseline_file(baseline_dir)[1]


def _read_baseline_file(baseline_dir: str) -> tuple[dict[tuple[str, str], str], str | None]:
    path = os.path.join(baseline_dir, _BASELINE_NAME)
    if not os.path.exists(path):
        return {}, None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    # v1 (bootstrap) was a bare list with no fingerprint; v2 wraps it:
    # {"engine_fingerprint": …, "books": […]} — a v1 file reads as fingerprint None.
    books = data if isinstance(data, list) else data["books"]
    fingerprint = None if isinstance(data, list) else data.get("engine_fingerprint")
    return (
        {(e["source_name"], e["canonical_he_title"]): e["hash"] for e in books},
        fingerprint,
    )


def write_snapshot_baseline(
    baseline_dir: str,
    hashes: dict[tuple[str, str], str],
    engine_fingerprint: str | None = None,
) -> None:
    books = [
        {"source_name": s, "canonical_he_title": t, "hash": h}
        for (s, t), h in sorted(hashes.items())
    ]
    data = {"engine_fingerprint": engine_fingerprint, "books": books}
    path = os.path.join(baseline_dir, _BASELINE_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=0, sort_keys=True)
        fh.write("\n")


def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_meta(repo: str, *, sefaria_export_tag, snapshot_sha256, book_count,
               ambiguity_policy="drop", bavli_convention=False, generated_at=None,
               engine_fingerprint=None) -> None:
    meta = {
        "schema_version": 3,
        "description": "Lineage of the last linker run (see stage 3). Source-change clock = snapshot.",
        "snapshot": {"sha256": snapshot_sha256, "book_count": book_count},
        "sefaria": {"export_tag": sefaria_export_tag},
        "engine": {"ambiguity_policy": ambiguity_policy, "bavli_convention": bavli_convention,
                   "fingerprint": engine_fingerprint},
        "generated_at": generated_at,
    }
    with open(os.path.join(repo, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# ── orchestration ────────────────────────────────────────────────────────────

def _log(msg):
    print(f"[incremental] {msg}", flush=True)


# ── progress reporting ───────────────────────────────────────────────────────
# Until 2026-09-06 the resolve stage's ONLY liveness signal was Sefaria's INFO
# firehose (98% of a 123 MB log, now silenced in link_books.quiet_sefaria_linker_logs).
# Silence without a substitute is worse than the flood: these pure formatters give the
# master one honest line per interval, derived from the done ledger and the plan — never
# from parsing a worker's log.

def _compact(count: int) -> str:
    """1558785 -> '1.56M', 380000 -> '380k'.  Line counts are read at a glance."""
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.0f}k"
    return f"{count / 1_000_000:.2f}M"


def _hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _hm(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}"


def format_progress(
    *,
    books_done: int,
    books_total: int,
    books_adopted: int = 0,
    lines_done: int = 0,
    lines_total: int = 0,
    elapsed: float,
    workers_alive: int,
    workers_replaced: int,
    workers_recycled_for_heavy: int = 0,
) -> str:
    """One progress line: N of M books, how long it has run, how long is left.

    ETA is a flat rate since the stage started — deliberately naive, because the only
    question it answers is "hours or minutes?".  The first interval has completed no
    book yet, so it reports `eta n/a` instead of dividing by zero or inventing a number.

    ``books_adopted`` are books the ledger already held when the stage STARTED (a
    --resume-checkpoints recovery adopts them, and a resumed NER attempt keeps its
    prior done markers).  They are real completed work, so they stay inside N/M and
    are named in the line rather than hidden — but this run did not compute them, and
    letting them into the rate makes the ETA nonsense: 4,000 books "done" in the first
    minute would predict the remaining 1,127 in seconds and keep under-predicting for
    hours.  The rate therefore divides by the books computed SINCE the stage started.
    """
    percent = (100.0 * books_done / books_total) if books_total else 0.0
    head = f"books {books_done}/{books_total} ({percent:.1f}%"
    head += f", {books_adopted} adopted)" if books_adopted else ")"
    parts = [head]
    if lines_total:
        parts.append(f"lines {_compact(lines_done)}/{_compact(lines_total)}")
    parts.append(f"elapsed {_hms(elapsed)}")
    computed = books_done - books_adopted
    if computed > 0 and elapsed > 0:
        remaining = (books_total - books_done) * (elapsed / computed)
        parts.append(f"eta ~{_hm(remaining)}")
    else:
        parts.append("eta n/a")
    workers = f"workers {workers_alive} alive, {workers_replaced} replaced"
    # Only when it happened: on a run with no deferred book the clause would be a
    # constant `0` on every line for hours.  When it is there it explains forks an
    # operator would otherwise read as instability (see link_books.HEAVY_FRESH_WORKER).
    if workers_recycled_for_heavy:
        workers += f", {workers_recycled_for_heavy} recycled-for-heavy"
    parts.append(workers)
    return "progress: " + " · ".join(parts)


def format_claim_summary(*, claimed: int, recovered: int, duplicates: int) -> str:
    """The claim protocol's whole story, once, when the resolve stage ends.

    `claimed` counts books this run's workers completed under an exclusive claim
    (books adopted from a checkpoint are excluded — they were never claimed here).
    `recovered` counts books taken over from a worker that died holding them: the
    number an operator needs to distinguish "peers covered a death" from "a book was
    dropped".  `duplicates` is 0 by construction now that a book is claimed by at most
    one worker; it is printed rather than assumed so a regression appears as a number
    instead of as an invariant nobody checks.  Relink 34016397157 would have printed
    `duplicates 2148` here.
    """
    return (
        f"claims: books claimed {claimed}, re-claimed after worker death {recovered}, "
        f"duplicates {duplicates}"
    )


def format_memory_summary(*, requeued: int, succeeded: int, failed: int) -> str | None:
    """One closing line for the MemoryError requeue policy, or None if it never fired.

    A relink where no book ran out of address space must not print a line about
    memory at all — a permanent `0 requeued` teaches an operator to skip the line
    that matters on the day it is not zero.  Relink 34016397157 would have printed
    `memory: 1 book requeued after MemoryError, 1 succeeded on retry, 0 failed`
    instead of dying on its 5,127th book.
    """
    if requeued <= 0:
        return None
    return (
        f"memory: {requeued} book{'' if requeued == 1 else 's'} requeued after "
        f"MemoryError, {succeeded} succeeded on retry, {failed} failed"
    )


def read_memory_stats(run_dir: str) -> dict:
    """Count the run's memory events from the engine's journal + the done/failed ledgers.

    `requeued` counts distinct BOOKS that spent their single retry (a book can be
    journalled twice: once when requeued, once when the retry also failed);
    `succeeded`/`failed` split them by the marker the run actually ended with, so the
    two always add up to `requeued` for a finished run.
    """
    # link_books is POSIX-only (resource/fcntl); the driver never runs where it is not
    # importable, and a caller on such a platform gets an honest set of zeroes.
    try:
        from link_books import count_memory_events, read_memory_journal
    except ImportError:
        return {"requeued": 0, "succeeded": 0, "failed": 0, "recycled_for_heavy": 0}

    requeued, seen = [], set()
    for event in read_memory_journal(run_dir):
        if event.get("event") != "requeued-after-memoryerror":
            continue
        cid = event.get("claim_id")
        if cid and cid not in seen:
            seen.add(cid)
            requeued.append(cid)
    failed_dir = os.path.join(run_dir, "failed")
    done_dir = os.path.join(run_dir, "done")
    failed = [cid for cid in requeued if os.path.exists(os.path.join(failed_dir, cid))]
    succeeded = [
        cid for cid in requeued
        if cid not in failed and os.path.exists(os.path.join(done_dir, cid))
    ]
    return {
        "requeued": len(requeued),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "recycled_for_heavy": count_memory_events(run_dir, "recycled-for-heavy"),
    }


def read_failed_notes(run_dir: str) -> dict[tuple[str, str], str]:
    """Per-book explanations the engine attached to its `failed` markers.

    Only the memory path writes one today.  The driver's RuntimeError is the
    ##[error] a human reads; a bare book title there would hide the growth figures
    and ceilings the engine already measured."""
    notes: dict[tuple[str, str], str] = {}
    failed_dir = os.path.join(run_dir, "failed")
    if not os.path.isdir(failed_dir):
        return notes
    for name in os.listdir(failed_dir):
        path = os.path.join(failed_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            continue
        note = payload.get("note")
        if isinstance(note, str) and note:
            notes[(payload["source_name"], payload["canonical_he_title"])] = note
    return notes


def format_done_line(
    *, total: int, adopted: int, computed: int, failed: int, checkpoint_source: str | None = None
) -> str:
    """The final line must never claim work this run did not do.

    Recovery 34021656701 adopted 5126 of 5127 books from a checkpoint, computed exactly
    one, and still reported `done: 5127 books re-linked` — which reads as five thousand
    books linked in ninety seconds.  Adopted and computed are now separate numbers.
    """
    breakdown = []
    if adopted:
        source = checkpoint_source or "local checkpoint"
        breakdown.append(f"adopted {adopted} from checkpoint {source}")
    breakdown.append(f"computed {computed}")
    breakdown.append(f"failed {failed}")
    return f"done: {total} books (" + ", ".join(breakdown) + ")"


# ── machine-readable run provenance (relink_work.json) ───────────────────────
# The `done:`/`claims:`/`memory:` lines above tell a HUMAN what the run did.  This
# object tells a MACHINE, and it ships beside the payload it describes.
#
# Recovery 34021656701 adopted 5,126 of 5,127 books from run 34016397157 and computed
# exactly one, in 7m12s -- and its published `relink_manifest.json` was indistinguishable
# from a full relink's: identity, digests and engine fingerprint, and not one number
# about the work.  Release linker-release-sha256-4632e6fb... therefore records no
# provenance at all for the run 99.98% of its content actually came from.
#
# WHY A SEPARATE FILE, not a `work` object inside relink_manifest.json: that manifest's
# key set is validated EXACTLY -- unknown keys are a hard failure -- in a DIFFERENT
# repository (SeforimLibrary .github/workflows/manual-generate-release.yml, the Phase-2
# "relink manifest key set mismatch" check) as well as in ci/validate_relink_manifest.py
# here.  Adding a key there breaks every build until both repos land together, and the
# recovery path must be able to run at the same head_sha as its checkpoint source.  This
# file has its own schema_version and no consumer yet, so it can grow additively.
WORK_SCHEMA_VERSION = 1

# ci/local_checkpoint_cache.py names a restored checkpoint `source-<run id>-<attempt>`
# and stamps it into <run dir>/checkpoint_source.txt.  Parsed, never trusted blindly:
# an unrecognised name keeps the raw string and reports the two ids as null rather than
# inventing a run number.
_CHECKPOINT_SOURCE_RE = re.compile(r"source-([1-9][0-9]*)-([1-9][0-9]*)\Z")

# GitHub's own coordinates for the run that is writing this file.  Bounded to 18
# digits so a hostile or corrupted environment cannot put an unbounded number into a
# published asset.
_RUN_COORDINATE_RE = re.compile(r"[1-9][0-9]{0,17}\Z")


def read_run_coordinates(environment=None) -> tuple[int | None, int | None]:
    """This run's GitHub coordinates, or ``(None, None)`` off a runner.

    Read from the environment rather than taken as a flag: there is then no second
    copy to keep in sync, and an operator running the driver on a laptop simply gets
    two nulls (which ci/validate_relink_work.py accepts).  Reported as a PAIR because
    half a coordinate names nothing.

    This is a SECOND self-report of an identity relink_manifest.json also carries, so
    it is made impossible for the two to disagree rather than merely unlikely: every
    CI call site of ci/validate_relink_work.py passes --run-id/--run-attempt, which
    requires these fields to be present AND equal to the same two numbers
    ci/validate_relink_manifest.py checks the manifest against, in the same publisher
    step, over the same handoff bytes.  Two files, one authority.
    """
    source = os.environ if environment is None else environment
    run = source.get("GITHUB_RUN_ID", "")
    attempt = source.get("GITHUB_RUN_ATTEMPT", "")
    if not _RUN_COORDINATE_RE.fullmatch(run) or not _RUN_COORDINATE_RE.fullmatch(attempt):
        return None, None
    return int(run), int(attempt)


def build_work_provenance(
    *,
    total: int,
    adopted: int,
    computed: int,
    failed: int,
    links_total: int,
    checkpoint_source: str | None = None,
    requeued: int = 0,
    reclaimed: int = 0,
    duplicates: int = 0,
    workers_replaced: int = 0,
    recycled_for_heavy: int = 0,
    generated_at: str | None = None,
    run_id: int | None = None,
    run_attempt: int | None = None,
) -> dict:
    """The whole shape of a relink as data.  Pure: every number is passed in.

    ``links_total`` is every link record in the PUBLISHED artifact store (all books,
    not only this run's plan) -- build_line_baseline counts it while it digests the
    store it is about to publish.  ``books_*`` describe this run's plan only, and
    ``adopted + computed + failed == total`` is checked by ci/validate_relink_work.py.

    A full relink with no checkpoint reports ``books_adopted == 0`` and all three
    ``checkpoint_source*`` fields null.  The converse is NOT an invariant: attempt
    34015274945 left a checkpoint of 280 shard files and ZERO completed books, so a
    named source with ``books_adopted == 0`` is a real, meaningful state.

    ``generated_at`` is the caller's timestamp (relink.yml passes --generated-at, the
    same one meta.json records); there is deliberately no clock in this module.
    ``run_id``/``run_attempt`` come from read_run_coordinates and make the asset
    answer "which run?" on its own, downloaded alone.
    """
    source_run_id = source_attempt = None
    if checkpoint_source:
        found = _CHECKPOINT_SOURCE_RE.fullmatch(checkpoint_source)
        if found:
            source_run_id = int(found.group(1))
            source_attempt = int(found.group(2))
    return {
        "schema_version": WORK_SCHEMA_VERSION,
        "generated_at": generated_at or None,
        "relink_run_id": None if run_id is None else int(run_id),
        "relink_run_attempt": None if run_attempt is None else int(run_attempt),
        "books_total": int(total),
        "books_adopted": int(adopted),
        "books_computed": int(computed),
        "books_failed": int(failed),
        "links_total": int(links_total),
        "checkpoint_source": checkpoint_source or None,
        "checkpoint_source_run_id": source_run_id,
        "checkpoint_source_attempt": source_attempt,
        "books_requeued_after_memoryerror": int(requeued),
        "books_reclaimed_after_worker_death": int(reclaimed),
        "duplicates": int(duplicates),
        "workers_replaced": int(workers_replaced),
        "workers_recycled_for_heavy": int(recycled_for_heavy),
    }


def read_work_counters(run_dir: str) -> dict:
    """The engine-side counters the closing `claims:`/`memory:` lines already report.

    Exactly the same readers, so the file and the log can never disagree.  Both are
    best-effort by construction (link_books is POSIX-only and the journals are
    diagnostics, never gates), which is why a platform without them reports honest
    zeroes instead of failing a finished relink.
    """
    memory = read_memory_stats(run_dir)
    reclaimed = duplicates = 0
    try:
        from link_books import count_claim_events
    except ImportError:
        pass
    else:
        reclaimed = count_claim_events(run_dir, "recovered")
        duplicates = count_claim_events(run_dir, "duplicate")
    return {
        "requeued": memory["requeued"],
        "recycled_for_heavy": memory["recycled_for_heavy"],
        "reclaimed": reclaimed,
        "duplicates": duplicates,
    }


def write_work_provenance(run_dir: str, value: dict) -> str:
    """Write ``<run dir>/relink_work.json`` atomically; return its path.

    Canonical JSON with one trailing LF, byte-for-byte the convention
    relink_manifest.json uses, so ci/validate_relink_work.py can reject anything the
    workflow reshaped on the way to the release.  Fail-closed on purpose: this file
    becomes a published release asset, and a swallowed write error would ship the
    PREVIOUS run's provenance from the durable run dir (which survives between runs
    on the self-hosted host) as if it described this one.
    """
    os.makedirs(run_dir, exist_ok=True)
    path = os.path.join(run_dir, "relink_work.json")
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    temporary = f"{path}.tmp-{os.getpid()}"
    with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return path


def format_work_provenance_line(path: str, value: dict) -> str:
    """One line, the same numbers, so the log and the asset are checkable against
    each other by eye.  `no checkpoint` is a fact about THIS run, not a permanent
    zero: it is exactly what distinguishes a real relink from an adoption."""
    if value.get("checkpoint_source_run_id"):
        source = (
            f"checkpoint from run {value['checkpoint_source_run_id']} "
            f"attempt {value['checkpoint_source_attempt']}"
        )
    elif value.get("checkpoint_source"):
        source = f"checkpoint from {value['checkpoint_source']}"
    else:
        source = "no checkpoint"
    return (
        f"provenance: wrote {path} (books {value['books_total']} = "
        f"adopted {value['books_adopted']} + computed {value['books_computed']} + "
        f"failed {value['books_failed']}, links {value['links_total']}, {source})"
    )


def compute_incremental_plan(args) -> dict:
    """Compute the immutable work plan without touching artifacts or baseline state.

    The GPU producer and CPU resolver both call this exact function.  Keeping one
    planner is a correctness boundary: a transport split must never let Kaggle
    recognize one set of books while the resolver advances a different set.
    """
    repo = os.path.abspath(args.repo)
    baseline_dir = os.path.join(repo, "baseline")
    # Diff the CURRENT snapshot's per-book content hashes against the baseline.
    #    A changed engine fingerprint invalidates the WHOLE baseline (full relink):
    #    the engine version shapes the output, so a partial relink under a new engine
    #    would leave the artifact store a mix of engines. The `removed` plan still uses
    #    the ORIGINAL baseline — a book that left the snapshot in the same cycle must
    #    have its artifact deleted, or the stale file survives the full relink.
    #    ONE-TIME MIGRATION: a v1 baseline (bootstrap, no fingerprint recorded) ADOPTS
    #    the current fingerprint without a full relink — the bootstrap artifacts are the
    #    known-good state and the workflow pins its engine to the bootstrap's commits.
    current = snapshot_book_hashes(args.snapshot)
    baseline, stored_fingerprint = _read_baseline_file(baseline_dir)
    fingerprint = getattr(args, "engine_fingerprint", None)
    changed, removed = plan_from_snapshot(current, baseline)
    reuse_engine_compatible = True
    if baseline and fingerprint is not None and stored_fingerprint != fingerprint:
        adopt = getattr(args, "adopt_fingerprint", None)
        if adopt:
            # Explicit OPERATOR-ATTESTED migration (same class as the one-time v1
            # adoption): the store's artifacts are known-good and the engine diff was
            # reviewed as output-neutral (failure handling / orchestration / logging /
            # transport only). Both sides must be pasted EXACTLY — a mismatch means the
            # operator is attesting a different migration than the one at hand.
            exp_old, sep, exp_new = adopt.partition("::")
            if not sep:
                raise RuntimeError("--adopt-fingerprint must be 'OLD::NEW' (both full strings)")
            if exp_old != stored_fingerprint or exp_new != fingerprint:
                raise RuntimeError(
                    "adoption attestation mismatch —\n"
                    f"  attested old: {exp_old!r}\n  stored   old: {stored_fingerprint!r}\n"
                    f"  attested new: {exp_new!r}\n  actual   new: {fingerprint!r}")
            _log(f"ADOPTING fingerprint by operator attestation (no full relink): "
                 f"{stored_fingerprint!r} -> {fingerprint!r}")
        elif stored_fingerprint is None:
            _log(f"baseline predates fingerprinting — ADOPTING {fingerprint!r} (no full relink)")
        elif getattr(args, "forbid_full_relink", False):
            # Serial mode: a waiting DB build cannot absorb an ~11h full relink (it
            # would time out mid-release). The engine change is deliberate — run the
            # standalone relink first, then re-run the weekly build.
            raise RuntimeError(
                "engine fingerprint changed "
                f"({stored_fingerprint!r} -> {fingerprint!r}) but a full relink is "
                "forbidden under a waiting build — dispatch relink.yml manually "
                "(standalone, no library_run_id) to migrate, then rerun the build")
        else:
            _log("engine fingerprint changed "
                 f"({stored_fingerprint!r} -> {fingerprint!r}) — FULL relink")
            changed = [BookKey(s, t) for (s, t) in sorted(current)]
            reuse_engine_compatible = False
    line_baseline_root = os.path.join(repo, LINE_BASELINE_DIRECTORY)
    previous_snapshot_sha = None
    try:
        with open(os.path.join(repo, "meta.json"), encoding="utf-8") as stream:
            previous_meta = json.load(stream)
        previous_snapshot_sha = previous_meta["snapshot"]["sha256"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        previous_snapshot_sha = None
    line_identity_valid = bool(
        reuse_engine_compatible
        and baseline
        and isinstance(previous_snapshot_sha, str)
        and validate_baseline_identity(
            line_baseline_root,
            snapshot_sha256=previous_snapshot_sha,
            engine_fingerprint=stored_fingerprint,
            book_count=len(baseline),
        )
    )
    line_deltas, reused_lines, ner_lines = plan_changed_books(
        args.snapshot,
        changed,
        baseline_root=line_baseline_root,
        baseline_hashes=baseline,
        baseline_identity_valid=line_identity_valid,
    )
    _log(f"snapshot: {len(current)} books | changed/new={len(changed)} removed={len(removed)}")
    if changed:
        mode = "exact line reuse" if line_identity_valid else "full-book fallback"
        _log(
            f"line plan ({mode}): reuse={reused_lines} NER={ner_lines} "
            f"({(100.0 * ner_lines / (reused_lines + ner_lines)) if reused_lines + ner_lines else 0:.3f}% GPU)"
        )
    return {
        "current": current,
        "changed": changed,
        "removed": removed,
        "line_deltas": line_deltas,
        "engine_fingerprint": fingerprint,
        "stored_engine_fingerprint": stored_fingerprint,
    }


def write_incremental_plan(args, output: str, relink_request_id: str) -> int:
    """Write the GPU→resolver planning contract as canonical, duplicate-free JSON."""
    import re

    if not re.fullmatch(r"[0-9a-f]{64}", relink_request_id or ""):
        raise RuntimeError("--relink-request-id must be exactly 64 lowercase hex characters")
    plan = compute_incremental_plan(args)
    current = plan["current"]
    document = {
        "schema_version": SCHEMA_VERSION,
        "relink_request_id": relink_request_id,
        "snapshot_sha256": sha256_of_file(args.snapshot),
        "changelog_sha256": sha256_of_file(args.changelog) if args.changelog else None,
        "engine_fingerprint": plan["engine_fingerprint"],
        "changed": [
            {
                **book.to_dict(),
                "ner_ranges": [
                    [start, end]
                    for start, end in plan["line_deltas"][
                        (book.source_name, book.canonical_he_title)
                    ].ner_ranges
                ],
            }
            for book in plan["changed"]
        ],
        "removed": [book.to_dict() for book in plan["removed"]],
        "current_books": [
            {"source_name": source, "canonical_he_title": title, "hash": digest}
            for (source, title), digest in sorted(current.items())
        ],
    }
    parent = os.path.dirname(os.path.abspath(output))
    os.makedirs(parent, exist_ok=True)
    tmp = output + ".tmp"
    with open(tmp, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, output)
    _log(f"wrote immutable split plan: {len(plan['changed'])} changed book(s) -> {output}")
    return len(plan["changed"])


def restore_completed_local_checkpoint(
    *, repo: str, snapshot: str, run_dir: str, changed_books_payload: list[dict]
) -> int:
    """Restore exact completed-book outputs and rebuild their done markers.

    The durable cache already binds every byte to the request, parent, snapshot and
    complete plan.  This second semantic gate proves that every cached record still
    names the exact planned book/line/content/context and has valid UTF-16 offsets
    before it can suppress work in a fresh engine invocation.
    """
    completed_path = os.path.join(run_dir, "completed_books.json")
    if not os.path.isfile(completed_path):
        return 0
    try:
        with open(completed_path, encoding="utf-8") as stream:
            completed = json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid completed-book checkpoint: {error}") from error
    if not isinstance(completed, list):
        raise RuntimeError("completed-book checkpoint must be an array")
    from link_books import claim_id
    plan = {
        (item["source_name"], item["canonical_he_title"]): item
        for item in changed_books_payload
    }
    seen = set()
    done_dir = os.path.join(run_dir, "done")
    os.makedirs(done_dir, exist_ok=True)
    con = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    restored = 0
    try:
        for index, item in enumerate(completed):
            if not isinstance(item, dict) or set(item) != {
                "claim_id", "source_name", "canonical_he_title", "hash", "artifact"
            }:
                raise RuntimeError(f"completed-book checkpoint entry {index} has an invalid shape")
            source_name = item["source_name"]
            title = item["canonical_he_title"]
            if not isinstance(source_name, str) or not source_name or not isinstance(title, str) or not title:
                raise RuntimeError(f"completed-book checkpoint entry {index} has an invalid book key")
            key = (source_name, title)
            planned = plan.get(key)
            if planned is None or item["hash"] != planned.get("hash"):
                raise RuntimeError(f"completed-book checkpoint differs from exact plan for {key!r}")
            book = BookKey(source_name, title)
            cid = claim_id(book)
            if item["claim_id"] != cid or cid in seen:
                raise RuntimeError(f"completed-book checkpoint claim mismatch/duplicate for {key!r}")
            seen.add(cid)
            artifact_name = item["artifact"]
            if artifact_name is not None and artifact_name != f"{cid}.jsonl":
                raise RuntimeError(f"completed-book checkpoint artifact mismatch for {key!r}")
            destination = os.path.join(repo, book_key_to_relpath(book))
            if artifact_name is None:
                try:
                    os.remove(destination)
                except FileNotFoundError:
                    pass
            else:
                cached = os.path.join(run_dir, "completed_artifacts", artifact_name)
                if not os.path.isfile(cached):
                    raise RuntimeError(f"completed-book artifact is missing for {key!r}")
                rows = con.execute(
                    "SELECT line_index, content, context_ref FROM lines_snapshot "
                    "WHERE source_name=? AND canonical_he_title=? ORDER BY line_index",
                    key,
                ).fetchall()
                current = {line_index: (content or "", context_ref) for line_index, content, context_ref in rows}
                records = list(read_artifact(cached))
                if not records:
                    raise RuntimeError(f"completed-book artifact is empty instead of absent for {key!r}")
                for record in records:
                    if record.book_key != book or record.line_index not in current:
                        raise RuntimeError(f"completed-book artifact identity differs for {key!r}")
                    content, context_ref = current[record.line_index]
                    if record.source_hash != content_hash(content):
                        raise RuntimeError(f"completed-book artifact source hash differs for {key!r}")
                    if record.context_ref is not None and record.context_ref != context_ref:
                        raise RuntimeError(f"completed-book artifact context differs for {key!r}")
                    if record.end > len(content.encode("utf-16-le")) // 2:
                        raise RuntimeError(f"completed-book artifact offset exceeds source for {key!r}")
                import shutil
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                temporary = f"{destination}.checkpoint-restore-{os.getpid()}"
                shutil.copy2(cached, temporary)
                os.replace(temporary, destination)
            open(os.path.join(done_dir, cid), "w").close()
            restored += 1
    finally:
        con.close()
    return restored


def read_checkpoint_source(run_dir: str) -> str | None:
    """Name of the checkpoint whose completed books this run may adopt, if any.

    ci/local_checkpoint_cache.py stamps it (`source-<run id>-<attempt>`) when it
    restores; absent for a run that starts from nothing.  Diagnostics only — the
    adoption gates are the manifest identity and the exact-plan comparison.
    """
    try:
        with open(os.path.join(run_dir, "checkpoint_source.txt"), encoding="utf-8") as stream:
            value = stream.read().strip()
    except OSError:
        return None
    return value or None


def run_incremental(args, summary: dict | None = None) -> int:
    """End-to-end incremental update. Returns the number of books in the plan.

    ``summary`` (optional, mutated in place) receives the honest breakdown of that
    number — ``total``/``adopted``/``computed``/``failed``/``checkpoint_source`` —
    so the caller's final line can distinguish books THIS run linked from books it
    adopted from a checkpoint.  The return value stays the plan size for callers
    that only care how many books the release covers.

    A successful run also writes ``<run dir>/relink_work.json`` — the same breakdown
    as data, for the release (see build_work_provenance).
    """
    repo = os.path.abspath(args.repo)
    baseline_dir = os.path.join(repo, "baseline")
    os.makedirs(baseline_dir, exist_ok=True)
    artifacts_dir = os.path.join(repo, "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)
    # The run dir survives between runs on the durable self-hosted host (it IS the
    # local checkpoint).  Drop any earlier run's provenance before this one starts, so
    # a file present at the end can only have been written by this run.
    try:
        os.remove(os.path.join(args.run_dir, "relink_work.json"))
    except OSError:
        pass
    plan = compute_incremental_plan(args)
    current = plan["current"]
    changed = plan["changed"]
    removed = plan["removed"]
    line_deltas = plan["line_deltas"]
    fingerprint = plan["engine_fingerprint"]

    # The release line baseline records whether each prior book had an artifact
    # and, when present, its exact digest. Validate that before any target-ref
    # rewrite mutates the store. Reusing an identical source line from a missing
    # or drifted artifact could otherwise turn corruption into silent link loss.
    for book in changed:
        delta = line_deltas[(book.source_name, book.canonical_he_title)]
        if not delta.reuse:
            continue
        artifact_path = os.path.join(repo, book_key_to_relpath(book))
        expected = delta.prior_artifact_sha256
        if expected is None:
            if os.path.exists(artifact_path):
                raise RuntimeError(
                    f"line baseline expected no prior artifact for "
                    f"{book.source_name}/{book.canonical_he_title!r}"
                )
        elif not os.path.isfile(artifact_path) or sha256_of_file(artifact_path) != expected:
            raise RuntimeError(
                f"prior artifact digest differs from the exact line baseline for "
                f"{book.source_name}/{book.canonical_he_title!r}"
            )

    # 2. Target en-renames: rewrite target_ref across ALL artifacts (no linking). Target-only.
    # The changelog_diff.json contract (SefariaExport generate_changelog.py) nests the book
    # diff under "books": {"new_tag":…, "books": {"en_renamed": […], …}, "versions": …}.
    changelog = {}
    if args.changelog and os.path.exists(args.changelog):
        with open(args.changelog, encoding="utf-8") as fh:
            changelog = json.load(fh)
    en_renamed = changelog.get("books", {}).get("en_renamed")
    if en_renamed:
        n = apply_en_renames(artifacts_dir, en_renamed)
        _log(f"rewrote target_ref on {n} records for {len(en_renamed)} en-renames")

    # 3. Drop artifacts for books that left the snapshot (deleted/renamed source).
    for bk in removed:
        if delete_source_artifact(repo, bk):
            _log(f"deleted artifact {bk.source_name}/{bk.canonical_he_title!r} (gone from snapshot)")

    # 4. Re-link only changed/new books, against the SAME snapshot we hashed.
    failed: set[tuple[str, str]] = set()
    codes: list[int] = []
    # Counters _run_engine owns (bounded worker replacements) and only it can see.
    engine_stats: dict = {}
    adopted = 0
    checkpoint_source = None
    if changed:
        os.makedirs(args.run_dir, exist_ok=True)
        changed_books_payload = [
            {
                **book.to_dict(),
                "hash": current[(book.source_name, book.canonical_he_title)],
                "ner_ranges": [
                    [start, end]
                    for start, end in line_deltas[
                        (book.source_name, book.canonical_he_title)
                    ].ner_ranges
                ],
                "reuse": [
                    [old_index, new_index]
                    for old_index, new_index in line_deltas[
                        (book.source_name, book.canonical_he_title)
                    ].reuse
                ],
            }
            for book in changed
        ]
        # Fresh engine ledger for THIS invocation. done/claim/failed markers are meaningful only
        # WITHIN one link_books.py run (poison-loop guard, worker claims, failure ledger). A stale
        # `done` from a reused run_dir would make the engine SKIP a changed book with no `failed`
        # marker → baseline would advance as if it linked → orphan. An explicitly restored
        # checkpoint may retain only immutable per-batch shards, and only when its complete
        # changed-book plan is byte-for-byte equivalent after JSON parsing. done/claim/failed are
        # always discarded; the fresh invocation reconstructs every completed book from verified
        # shards and writes new done markers. (logs/ is append-only diagnostics — kept.)
        import shutil
        resume_checkpoints = bool(getattr(args, "resume_checkpoints", False))
        # heavy/ and heavy-slots/ are this run's deferral decisions and slot locks; a
        # stale marker from another request would send a book straight to the heavy
        # phase, so they are recomputed from scratch like the rest of the ledger.
        # claim-events/ is this invocation's tally of takeovers and duplicate
        # completions; carrying one over would make the closing summary a lie.
        # memory/ holds this invocation's MemoryError journal and its one-per-book
        # requeue markers: a marker carried over would silently deny a book its retry
        # in a run that has not yet tried it once.
        from link_books import CLAIM_EVENT_DIR, MEMORY_DIR
        for d in ("done", "claim", "failed", "heavy", "heavy-slots", CLAIM_EVENT_DIR,
                  MEMORY_DIR):
            shutil.rmtree(os.path.join(args.run_dir, d), ignore_errors=True)
        only = os.path.join(args.run_dir, "changed_books.json")
        if resume_checkpoints:
            try:
                with open(only, encoding="utf-8") as fh:
                    restored_payload = json.load(fh)
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    "--resume-checkpoints requires a valid restored changed_books.json"
                ) from error
            if restored_payload != changed_books_payload:
                raise RuntimeError(
                    "restored checkpoint plan differs from the exact current incremental plan"
                )
            if not os.path.isdir(os.path.join(args.run_dir, "checkpoints")):
                raise RuntimeError("--resume-checkpoints requires a restored checkpoints directory")
            _log("accepted exact-plan local checkpoint; stale ledgers were discarded")
            checkpoint_source = read_checkpoint_source(args.run_dir)
            adopted = restore_completed_local_checkpoint(
                repo=args.repo,
                snapshot=args.snapshot,
                run_dir=args.run_dir,
                changed_books_payload=changed_books_payload,
            )
            # State the shape of the remaining work up front: an operator watching a
            # recovery must be able to tell "5126 books already done, 1 left" from
            # "5127 books to link" before the engine starts.
            _log(
                f"restored checkpoint {checkpoint_source or '(unnamed)'}: "
                f"{adopted}/{len(changed)} books complete → {len(changed) - adopted} to compute"
            )
        else:
            shutil.rmtree(os.path.join(args.run_dir, "checkpoints"), ignore_errors=True)
            shutil.rmtree(os.path.join(args.run_dir, "completed_artifacts"), ignore_errors=True)
            try:
                os.remove(os.path.join(args.run_dir, "completed_books.json"))
            except FileNotFoundError:
                pass
            with open(only, "w", encoding="utf-8") as fh:
                json.dump(changed_books_payload, fh, ensure_ascii=False)
        requested_ner_lines = sum(
            line_deltas[(book.source_name, book.canonical_he_title)].ner_line_count
            for book in changed
        )
        reused_lines = sum(
            line_deltas[(book.source_name, book.canonical_he_title)].reused_line_count
            for book in changed
        )
        _log(
            f"updating {len(changed)} changed books: "
            f"{requested_ner_lines} line(s) require NER, {reused_lines} reused"
        )
        codes = _run_engine(args, only, stats=engine_stats)
        failed = read_failed_books(args.run_dir)

        # Completeness assertion: every requested book must carry a `done` marker.
        # A book can otherwise slip through with NO marker at all (e.g. its claim was
        # held during a NER outage while every worker walked past it) — the engine
        # exits 0 and the baseline would advance over a book that was never linked.
        from link_books import claim_id
        done_dir = os.path.join(args.run_dir, "done")
        done = set(os.listdir(done_dir)) if os.path.isdir(done_dir) else set()
        missing = [b for b in changed if claim_id(b) not in done]
        if missing:
            raise RuntimeError(
                f"{len(missing)} requested book(s) finished with neither done nor "
                "failed marker — refusing to advance the baseline"
                + (f" (worker exit codes {codes})" if any(codes) else "") + ": "
                + ", ".join(sorted(f"{b.source_name}/{b.canonical_he_title}" for b in missing)))


    # A per-book crash FAILS the whole run, loudly. In the serial pipeline the build waits
    # on this run and injects its output into the DB being released — a missing book would
    # ship a silently-incomplete link set (a NEW book leaves nothing for linkerStrict to
    # even hash-check). Baseline/meta are NOT advanced, so a rerun retries everything.
    if failed:
        # Quote the engine's own note where it left one (today: the MemoryError path,
        # which already measured both attempts' growth and the ceilings in force). The
        # 2026-09-06 ##[error] named only the book, and the same book linked in 2.3 s
        # in the next run — the numbers are what tell those two cases apart.
        notes = read_failed_notes(args.run_dir)
        raise RuntimeError(
            f"{len(failed)} book(s) failed inside the engine — failing the run "
            "(baseline not advanced"
            + (f"; worker exit codes {codes}" if any(codes) else "") + "): "
            + ", ".join(
                f"{s}/{t}" + (f" [{notes[(s, t)]}]" if (s, t) in notes else "")
                for s, t in sorted(failed)
            ))

    # The ledger is the sole authority on output state: write_artifact is atomic and a
    # done marker lands only after the book's artifact did. A worker that died (kernel
    # OOM on a huge book) whose books were then finished by peers via stale-claim steal
    # left no hole — failing here would only force a from-zero rerun of a multi-hour
    # relink. Deaths with a hole are already fatal above (missing/failed carry the codes).
    if any(codes):
        _log(f"WARNING: {sum(1 for c in codes if c)} engine worker(s) died "
             f"(exit codes {codes}) but every requested book carries a clean done "
             "marker — peers covered the dead workers' books; proceeding on the ledger")

    # 5. Advance baseline + lineage. Any failure never reaches here (raised above), so the
    #    baseline advances to exactly the snapshot hashes that were fully linked.
    snapshot_sha256 = sha256_of_file(args.snapshot)
    # Build the next exact reuse baseline before advancing the committed book clock.
    # A failure here leaves both clocks on the previous accepted release.
    links_total = build_line_baseline(
        args.snapshot,
        os.path.join(repo, LINE_BASELINE_DIRECTORY),
        current_hashes=current,
        snapshot_sha256=snapshot_sha256,
        engine_fingerprint=fingerprint,
        artifacts_root=artifacts_dir,
    )
    write_snapshot_baseline(baseline_dir, dict(current), engine_fingerprint=fingerprint)
    write_meta(
        repo,
        sefaria_export_tag=args.sefaria_tag,
        snapshot_sha256=snapshot_sha256,
        book_count=len(current),
        bavli_convention=args.bavli_convention,
        generated_at=args.generated_at,
        engine_fingerprint=fingerprint,
    )
    _log("book baseline + exact line baseline + meta.json updated")
    if getattr(args, "ner_bundle_dir", None):
        # Cooperative resolver shards are recovery state, not release state. Keep
        # them through every fail-closed gate above; remove only after both baselines
        # and metadata have advanced successfully.
        import shutil
        shutil.rmtree(os.path.join(args.run_dir, "checkpoints"), ignore_errors=True)
    # 6. Record what this run actually DID, as data, next to what it produced. Written
    #    last and only here: every fail-closed gate above is behind us, so the file
    #    exists exactly when a payload was published, and describes that payload.
    #    The engine journals describe THIS run's invocation of the engine.  They are
    #    wiped with the rest of the ledger inside `if changed:` above, so a run with an
    #    EMPTY delta never clears them — and on the durable run dir this function
    #    already guards against (the unlink at entry) it would otherwise publish the
    #    previous run's requeues, re-claims, duplicates and heavy recycles under
    #    `books_total: 0`.  No engine ran, so the honest report is zero, exactly as
    #    `engine_stats` already reports for workers_replaced.
    counters = (
        read_work_counters(os.path.abspath(args.run_dir)) if changed
        else {"requeued": 0, "recycled_for_heavy": 0, "reclaimed": 0, "duplicates": 0}
    )
    provenance_run_id, provenance_run_attempt = read_run_coordinates()
    work = build_work_provenance(
        total=len(changed),
        adopted=adopted,
        computed=len(changed) - adopted,
        failed=len(failed),
        links_total=links_total,
        checkpoint_source=checkpoint_source,
        requeued=counters["requeued"],
        reclaimed=counters["reclaimed"],
        duplicates=counters["duplicates"],
        workers_replaced=int(engine_stats.get("workers_replaced", 0)),
        recycled_for_heavy=counters["recycled_for_heavy"],
        # The same timestamp meta.json records, so the two agree by construction.
        generated_at=getattr(args, "generated_at", None),
        run_id=provenance_run_id,
        run_attempt=provenance_run_attempt,
    )
    _log(format_work_provenance_line(write_work_provenance(args.run_dir, work), work))
    if summary is not None:
        # `failed` is empty here by construction (a non-empty ledger raised above);
        # reporting the zero is the point — the operator sees it was checked.
        summary.update(
            total=len(changed),
            adopted=adopted,
            computed=len(changed) - adopted,
            failed=len(failed),
            checkpoint_source=checkpoint_source,
            work=work,
        )
    return len(changed)


def install_terminate_handler():
    """Convert SIGTERM/SIGINT into SystemExit so `finally` blocks run.

    Without this, a TERM (job cancel) kills the driver outright and the engine
    worker process groups it owns become orphans — free to outlive the run (and,
    holding the inherited lease fd, to block the next heavy phase)."""
    import signal

    def _raise(signum, frame):
        # The first signal transfers control to the normal finally path.  A second
        # TERM/INT (runner escalation is allowed to send more than one) must not
        # interrupt that finally block before it reaches SIGKILL + wait().
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _raise)
    signal.signal(signal.SIGINT, _raise)


def _alive_worker_count(heartbeat_dir: str, exclude: set, stall: float, now: float,
                        labels: set | None = None) -> int:
    """Resolvers whose heartbeat is fresh, judged on the SAME files the supervisor uses.

    In pool mode this driver spawns one master but the forked children each keep their
    own `w01…` heartbeat, so counting files (minus the master's label) reports the real
    resolver count in both modes.  A worker between heartbeats is not "dead" until the
    supervisor would say so, hence the same stall window.
    """
    try:
        names = os.listdir(heartbeat_dir)
    except OSError:
        return 0
    alive = 0
    for name in names:
        if name in exclude or (labels is not None and name not in labels):
            continue
        try:
            if now - os.path.getmtime(os.path.join(heartbeat_dir, name)) <= stall:
                alive += 1
        except OSError:
            continue
    return alive


def _heartbeat_namespace() -> str:
    """A run-private heartbeat directory name, safe to pass to the engine.

    A durable ``--run-dir`` can retain fresh-looking heartbeat files from an earlier
    invocation.  Giving each driver invocation its own unguessable subdirectory means
    a progress line can only count the workers this invocation started.
    """
    import secrets
    return secrets.token_hex(16)


def _process_group_members(pgid: int, token: str, leader=None) -> tuple[list[int], list[int]]:
    """Return live token-bearing and unverified members of this session/group.

    ``start_new_session`` makes the Popen leader both PGID and SID.  If that leader is
    still waitable (alive or a zombie observed with ``waitid(WNOWAIT)``), its PID cannot
    be reused; that pins the numeric PGID/SID and safely binds every same-UID member.
    After a leader has been reaped, the inherited random environment marker remains the
    fail-closed ownership proof.  A same-number group without either proof is never
    signalled: that is the PID/PGID-reuse safety boundary.
    """
    owned, unverified = [], []
    marker = f"LINKER_ENGINE_SESSION_TOKEN={token}".encode()
    try:
        entries = os.scandir("/proc")
    except OSError:
        # On a host without /proc, a non-existent group is harmless, but an existing
        # leaderless group cannot be attributed safely.  The one safe fallback is an
        # unreaped Popen leader we just created: its PID cannot yet be reused and
        # start_new_session made it the sole session/group leader.  Do not turn the
        # leaderless case into a test-only no-op: callers refuse replacement for it.
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return owned, unverified
        except PermissionError:
            return owned, [pgid]
        # Do not call poll() here: while our child is unreaped its PID cannot be
        # reused, so ``returncode is None`` is the safe identity binding.  Calling
        # poll would reap a just-killed leader and erase that binding before its
        # descendants had been drained.
        if leader is not None and leader.returncode is None:
            return [pgid], unverified
        return owned, [pgid]
    leader_bound = (
        leader is not None
        and hasattr(leader, "_waitpid_lock")
        and getattr(leader, "pid", None) == pgid
        and getattr(leader, "returncode", None) is None
    )
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                with open(os.path.join(entry.path, "stat"), encoding="utf-8") as stream:
                    tail = stream.read().rsplit(")", 1)[1].strip().split()
                # tail starts at proc field 3: state, ppid, pgrp, session, ...
                if tail[0] == "Z" or int(tail[2]) != pgid or int(tail[3]) != pgid:
                    continue
                if os.stat(entry.path).st_uid != os.geteuid():
                    unverified.append(int(entry.name))
                    continue
                # A live or unreaped-zombie child created by this Popen still owns its
                # PID in the kernel.  Because start_new_session made that same number
                # the PGID and SID, no unrelated group can reuse it while the leader is
                # waitable.  This path also covers hardened procfs mounts that deny
                # access to environ for a same-UID child.
                if leader_bound:
                    owned.append(int(entry.name))
                    continue
                with open(os.path.join(entry.path, "environ"), "rb") as stream:
                    environment = stream.read().split(b"\0")
                if marker in environment:
                    owned.append(int(entry.name))
                else:
                    unverified.append(int(entry.name))
            except (FileNotFoundError, ProcessLookupError):
                # A member that vanished during inspection needs no signal.
                continue
            except (PermissionError, OSError, IndexError, ValueError):
                # We already selected this entry's session/group; if it is still
                # visible but cannot be authenticated, signalling the whole PGID
                # would make a PID/PGID reuse mistake possible.
                unverified.append(int(entry.name))
    return owned, unverified


def _poll_engine_without_reaping(proc):
    """Return a Linux child status while retaining its PID as the group identity.

    ``Popen.poll()`` reaps a dead leader.  A pool master's forked children can remain
    in its PGID after that death, so reaping first discards the strongest race-free
    proof that the numeric PGID is still ours.  Linux ``waitid(..., WNOWAIT)`` observes
    the status but deliberately leaves the child waitable until group cleanup ends.
    Synthetic Popen objects and non-Linux hosts keep the ordinary polling path.
    """
    if (
        sys.platform.startswith("linux")
        and hasattr(os, "waitid")
        and hasattr(os, "WNOWAIT")
        and hasattr(proc, "_waitpid_lock")
    ):
        try:
            status = os.waitid(
                os.P_PID,
                proc.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except (ChildProcessError, OSError):
            # If some external code already consumed the child, fall back to Popen's
            # state.  The token-based scanner remains fail-closed after a reap.
            return proc.poll()
        if status is None:
            return None
        if status.si_code == os.CLD_EXITED:
            return status.si_status
        return -status.si_status
    return proc.poll()


def _terminate_owned_group(group: dict, grace: float, log) -> bool:
    """Stop and wait for one registered process group without trusting Popen.poll().

    The return value is true only after every live process in the recorded group has
    disappeared.  Refusing an unverified group is intentional: replacement must not
    start beside workers whose ownership cannot be proved.
    """
    import signal
    import subprocess
    import time

    pgid, token = group["pgid"], group["token"]

    def members():
        owned, unverified = _process_group_members(pgid, token, group.get("proc"))
        if unverified:
            raise RuntimeError(
                f"refusing to signal process group {pgid}: its session contains "
                f"unverified member(s) {unverified}"
            )
        return owned

    live = members()
    if not live:
        return True
    if not os.path.isdir("/proc"):
        # The authenticated leader has not been reaped yet, so its PGID cannot be
        # reused while TERM/KILL is sent.  Once wait() reaps it, only a missing group
        # is success; an extant leaderless group is deliberately reported unsafe.
        leader = group.get("proc")
        if leader is None:
            raise RuntimeError(f"cannot authenticate process group {pgid} without /proc")
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        try:
            leader.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                return True
            try:
                leader.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return False
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return not members()
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not members():
            return True
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return not members()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not members():
            return True
        time.sleep(0.1)
    live = members()
    log(f"process group {pgid} survived SIGKILL: members {live}")
    return False


def _run_engine(args, only_books_path, progress_seconds=60.0, stats=None):
    """Invoke link_books.py on the changed subset, in the Sefaria-Project venv/cwd.

    --engine-workers N runs N engine processes in parallel — they coordinate through the
    run-dir claim ledger, so this is the same mechanism the bootstrap used. Returns the
    workers' exit codes; the caller judges them against the ledger (a dead worker whose
    books were finished by peers via stale-claim steal is not a failure).

    ``stats`` (optional, mutated in place) receives ``workers_replaced`` — the bounded
    replacements THIS driver made, which only this frame can see (a pool child's
    recycles are the master's, and it logs each one).  Set on every exit path so a run
    that dies here still reports what it had spent.

    Process ownership: every worker starts in its OWN session/process group
    (start_new_session), so on any exit path — including SIGTERM/SIGINT via
    install_terminate_handler — the finally block signals exactly the groups this
    driver created (TERM, bounded wait, then KILL) and nothing else on the host.
    If the host-lease fd (9) is open it is passed through to the workers as
    defence in depth: a worker that outlives a dying driver keeps the lease held,
    so a new heavy phase cannot start beside it; the relink-start reaper is what
    then clears such orphans."""
    import signal
    import subprocess
    import time
    engine = os.path.join(os.path.dirname(os.path.abspath(__file__)), "link_books.py")
    base_cmd = [args.python, engine,
                # abspath everything: workers run with cwd=sef_project, so any
                # workspace-relative path (e.g. inputs/lines_snapshot.db) breaks there.
                "--snapshot", os.path.abspath(args.snapshot), "--repo", os.path.abspath(args.repo),
                "--run-dir", os.path.abspath(args.run_dir), "--only-books", os.path.abspath(only_books_path)]
    if args.bavli_convention:
        base_cmd.append("--bavli-convention")
    ner_bundle_dir = getattr(args, "ner_bundle_dir", None)
    if ner_bundle_dir:
        base_cmd += [
            "--ner-bundle-dir", os.path.abspath(ner_bundle_dir),
            "--expected-engine-fingerprint", args.engine_fingerprint or "",
            "--expected-relink-request-id", args.relink_request_id or "",
        ]
    env = dict(os.environ, PYTHONPATH=args.sef_project + ":" + os.environ.get("PYTHONPATH", ""))
    workers = max(1, int(getattr(args, "engine_workers", 1) or 1))
    restart_limit = max(0, int(getattr(args, "engine_restart_limit", 2) or 0))
    stall = float(getattr(args, "worker_stall_seconds", 1800) or 1800)
    # --engine-pool: ONE engine process loads the Sefaria library and forks the workers
    # from it (copy-on-write), instead of N processes each loading their own ~2 GB copy.
    # The master supervises its children (link_books.run_pool) with the same heartbeat,
    # stall and bounded-replacement contract; this driver supervises only the master,
    # whose heartbeat label is "pool".
    pool = bool(getattr(args, "engine_pool", False))
    if pool:
        base_cmd += ["--pool-workers", str(workers)]
        env["LINKER_POOL_RESTART_LIMIT"] = str(restart_limit)
        env["LINKER_POOL_STALL_SECONDS"] = str(stall)
    _log(f"running engine ({workers} worker(s){', pooled' if pool else ''}): " + " ".join(base_cmd))
    lease_fds = ()
    try:
        os.fstat(9)
        lease_fds = (9,)
    except OSError:
        pass
    if pool:
        worker_labels = ["pool"]
    else:
        worker_labels = ["w1"] if workers == 1 else [f"w{n:02d}" for n in range(1, workers + 1)]
    # Heartbeats are advisory liveness, but their count is operator-facing.  A durable
    # run directory may still hold fresh mtimes from an interrupted older invocation;
    # this private namespace makes those files invisible to this invocation.
    heartbeat_namespace = _heartbeat_namespace()
    env["LINKER_HEARTBEAT_NAMESPACE"] = heartbeat_namespace
    heartbeat_dir = os.path.join(
        os.path.abspath(args.run_dir), "worker-heartbeats", heartbeat_namespace
    )
    os.makedirs(heartbeat_dir, exist_ok=True)
    # Remove the legacy flat names for the labels we are about to supervise.  The
    # private namespace is the correctness boundary; this keeps an interrupted older
    # invocation from misleading people inspecting the directory by hand as well.
    legacy_heartbeat_dir = os.path.dirname(heartbeat_dir)
    legacy_heartbeat_labels = set(worker_labels)
    if pool:
        legacy_heartbeat_labels.update(
            f"w{number:02d}" for number in range(1, workers + 1)
        )
    for label in legacy_heartbeat_labels:
        try:
            os.remove(os.path.join(legacy_heartbeat_dir, label))
        except FileNotFoundError:
            pass

    # The exact done ledger lets the supervisor avoid a pointless replacement if a
    # worker happens to die after peers already completed the requested plan.  Keep
    # direct unit-test callers with synthetic paths compatible; production always
    # receives the JSON file written immediately above in run_incremental().
    expected_done = None
    # Planned NER lines per claim id: the second axis of the progress line, so a run
    # whose remaining books are the giant ones does not look stalled at 95% of books.
    planned_lines = {}
    try:
        from link_books import claim_id
        with open(only_books_path, encoding="utf-8") as fh:
            requested = json.load(fh)
        expected_done = set()
        for item in requested:
            cid = claim_id(BookKey(item["source_name"], item["canonical_he_title"]))
            expected_done.add(cid)
            planned_lines[cid] = sum(
                end - start for start, end in (item.get("ner_ranges") or ())
            )
    except (OSError, ValueError, KeyError, TypeError):
        expected_done = None
        planned_lines = {}

    def ledger_complete():
        done_dir = os.path.join(os.path.abspath(args.run_dir), "done")
        return expected_done is not None and all(
            os.path.isfile(os.path.join(done_dir, cid)) for cid in expected_done
        )

    procs = []
    owned_groups = []
    groups_by_proc = {}
    scope_paths = []
    active = {}
    spawned_at = {}
    restart_counts = {label: 0 for label in worker_labels}
    scope_dir = os.environ.get("LINKER_ENGINE_SCOPE_DIR")
    scope_tool = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "ci", "process_scope.py",
    )
    term_grace = float(os.environ.get("LINKER_PROCESS_TERM_GRACE", "15"))

    def spawn_worker(label):
        import secrets

        session_token = secrets.token_hex(32)
        proc_env = dict(env, LINKER_ENGINE_SESSION_TOKEN=session_token)
        proc = subprocess.Popen(
            base_cmd + ["--label", label],
            cwd=args.sef_project,
            env=proc_env,
            start_new_session=True,
            pass_fds=lease_fds,
        )
        # Record ownership immediately.  Every historical process remains in procs
        # so the final cleanup also reaps a replacement's predecessor.
        procs.append(proc)
        group = {
            "pgid": proc.pid, "token": session_token, "label": label, "proc": proc,
        }
        owned_groups.append(group)
        groups_by_proc[id(proc)] = group
        active[label] = proc
        spawned_at[proc.pid] = time.time()
        if scope_dir:
            os.makedirs(scope_dir, exist_ok=True)
            restart = restart_counts[label]
            state = os.path.join(scope_dir, f"engine-{label}-r{restart:02d}.json")
            subprocess.run([
                sys.executable, scope_tool, "record", "--state", state,
                "--pid", str(proc.pid), "--kind", f"linker-engine-{label}",
                "--expect", "link_books.py",
            ], check=True)
            scope_paths.append((state, proc))
        return proc

    done_dir = os.path.join(os.path.abspath(args.run_dir), "done")
    # Pool children write their own w01… heartbeats beside the master's; counting the
    # master would report "1 alive" while twelve resolvers work.  The namespace above
    # already excludes prior invocations; this label allow-list also ignores unrelated
    # files somebody places beside a current worker.
    master_labels = set(worker_labels) if pool else set()
    heartbeat_labels = (
        {f"w{number:02d}" for number in range(1, workers + 1)} if pool else set(worker_labels)
    )

    def ledger_done():
        if expected_done is None:
            return set()
        try:
            return expected_done & set(os.listdir(done_dir))
        except OSError:
            return set()

    # Books already complete before a single worker was spawned.  run_incremental wipes
    # done/ and then, on --resume-checkpoints, re-writes a marker per book adopted from
    # the checkpoint, so this is exactly the adopted count.  Kept out of the ETA rate
    # (see format_progress) and named in the line so "5126/5127" cannot read as work
    # this run performed.
    adopted_at_start = len(ledger_done())

    def report_progress(now, started_at):
        if expected_done is None:
            return  # synthetic/unit-test invocation: no plan to measure against
        done = ledger_done()
        _log(format_progress(
            books_done=len(done),
            books_total=len(expected_done),
            books_adopted=adopted_at_start,
            lines_done=sum(planned_lines.get(cid, 0) for cid in done),
            lines_total=sum(planned_lines.values()),
            elapsed=now - started_at,
            workers_alive=_alive_worker_count(
                heartbeat_dir, master_labels, stall, now, heartbeat_labels
            ),
            # Pool-child recycles are the master's business and it logs each one
            # (`pool: wNN recycled`); this counts only the replacements THIS driver made.
            workers_replaced=sum(restart_counts.values()),
            # One journal read per interval (a handful of lines): a heavy phase forks
            # a fresh worker per deferred book, and that must not read as churn.
            workers_recycled_for_heavy=read_memory_stats(
                os.path.abspath(args.run_dir)
            )["recycled_for_heavy"],
        ))

    def report_claims():
        """One closing line for the claim protocol (see format_claim_summary)."""
        if expected_done is None:
            return  # synthetic/unit-test invocation: no plan, nothing was claimed
        try:
            from link_books import count_claim_events
        except ImportError:
            return
        run = os.path.abspath(args.run_dir)
        _log(format_claim_summary(
            claimed=max(0, len(ledger_done()) - adopted_at_start),
            recovered=count_claim_events(run, "recovered"),
            duplicates=count_claim_events(run, "duplicate"),
        ))

    def report_memory():
        """The MemoryError requeue tally, only when a book actually needed it."""
        if expected_done is None:
            return  # synthetic/unit-test invocation: no plan, nothing was linked
        stats = read_memory_stats(os.path.abspath(args.run_dir))
        line = format_memory_summary(
            requeued=stats["requeued"],
            succeeded=stats["succeeded"],
            failed=stats["failed"],
        )
        if line is not None:
            _log(line)

    run_error = None
    try:
        # Append immediately after every successful spawn.  A list comprehension
        # loses all already-created children if a later Popen raises before the
        # assignment completes, leaving an unowned worker/process group behind.
        for label in worker_labels:
            spawn_worker(label)

        codes = []
        # The resolve stage runs for hours with no other output now that Sefaria's
        # INFO firehose is silenced.  One line per interval, from the done ledger.
        stage_started_at = time.time()
        next_progress = stage_started_at + progress_seconds
        while active:
            time.sleep(1)
            now = time.time()
            if now >= next_progress:
                next_progress = now + progress_seconds
                report_progress(now, stage_started_at)
            for label, proc in list(active.items()):
                code = _poll_engine_without_reaping(proc)
                if code is None:
                    path = os.path.join(heartbeat_dir, label)
                    try:
                        last = max(spawned_at[proc.pid], os.path.getmtime(path))
                    except OSError:
                        last = spawned_at[proc.pid]
                    if now - last <= stall:
                        continue
                    _log(
                        f"worker {label} pid={proc.pid} has no heartbeat for "
                        f"{now-last:.0f}s; terminating its owned group"
                    )
                    group = groups_by_proc[id(proc)]
                    if not _terminate_owned_group(group, term_grace, _log):
                        raise RuntimeError(
                            f"owned engine group {proc.pid} survived the stall cleanup"
                        )
                    code = proc.wait()

                if code is None:
                    continue
                codes.append(code)
                del active[label]
                # A SIGKILLed pool master can leave forked resolvers in its old PGID.
                # Drain that *registered, token-bound* group before even considering a
                # replacement, so no old generation can overlap the next one.
                group = groups_by_proc[id(proc)]
                if not _terminate_owned_group(group, term_grace, _log):
                    raise RuntimeError(
                        f"owned engine group {proc.pid} survived after leader exit {code}"
                    )
                # On Linux the status above was observed with WNOWAIT so the leader's
                # PID could pin its PGID/SID throughout cleanup.  Reap only now, before
                # considering a replacement.
                if getattr(proc, "returncode", None) is None:
                    code = proc.wait()
                if code == 0 or ledger_complete():
                    continue
                if restart_counts[label] >= restart_limit:
                    _log(
                        f"worker {label} exited {code}; bounded replacement limit "
                        f"{restart_limit} exhausted — peers may still complete its book"
                    )
                    continue
                restart_counts[label] += 1
                _log(
                    f"worker {label} exited {code} before ledger completion; "
                    f"starting bounded replacement {restart_counts[label]}/{restart_limit}"
                )
                spawn_worker(label)
        # Final tally: the last interval may be up to progress_seconds stale, and the
        # completeness assertion that follows reads better after a stated N/M.
        report_progress(time.time(), stage_started_at)
        report_claims()
        report_memory()
    except BaseException as error:
        # Cleanup must run on every exception, but a cleanup diagnostic is never more
        # useful than the engine failure that triggered it.  Preserve that original
        # exception below and log any secondary cleanup failure beside it.
        run_error = error
        raise
    finally:
        # Deliberately visit *every registered group*, not just Popen objects whose
        # leader is still alive.  A pool child remains in the old session after its
        # master is killed, exactly the state Popen.poll() cannot represent.
        cleanup_failures = []
        for group in owned_groups:
            try:
                if not _terminate_owned_group(group, term_grace, _log):
                    cleanup_failures.append(f"process group {group['pgid']} survived SIGKILL")
            except Exception as error:
                cleanup_failures.append(str(error))
        # Reap every leader we created.  Group cleanup above is deliberately
        # independent of this wait: a dead leader may already have been reaped while
        # descendants were still live.
        for p in procs:
            try:
                p.wait(timeout=5)
            except (subprocess.TimeoutExpired, ChildProcessError, OSError):
                cleanup_failures.append(f"engine leader {p.pid} did not exit after group cleanup")
        for state, proc in scope_paths:
            try:
                owned, unverified = _process_group_members(
                    groups_by_proc[id(proc)]["pgid"], groups_by_proc[id(proc)]["token"], proc
                )
                if not owned and not unverified:
                    try:
                        os.remove(state)
                    except FileNotFoundError:
                        pass
                else:
                    _log(
                        f"retaining process scope {state}: group {proc.pid} still has "
                        f"owned members {owned} or unverified members {unverified}"
                    )
            except Exception as error:
                cleanup_failures.append(f"could not inspect process scope {state}: {error}")
        if stats is not None:
            stats["workers_replaced"] = sum(restart_counts.values())
        if cleanup_failures:
            message = "engine process-group cleanup failed: " + "; ".join(cleanup_failures)
            if run_error is None:
                raise RuntimeError(message)
            _log(message + f" (preserving original {type(run_error).__name__})")
    if any(codes):
        _log(f"engine worker exit codes: {codes} — deferring judgment to the ledger")
    return codes


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Incremental linker driver (stage 3)")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--snapshot", required=True,
                    help="lines_snapshot.db the linker links — the sole source-change clock")
    ap.add_argument("--changelog", default=None,
                    help="changelog_diff.json — target_ref rewrite ONLY, not source detection")
    ap.add_argument("--sefaria-tag", default=None, help="Sefaria export tag (lineage/target side)")
    ap.add_argument("--generated-at", default=None, help="ISO timestamp (passed in; no clock here)")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--sef-project", required=True, help="Sefaria-Project dir (engine cwd/PYTHONPATH)")
    ap.add_argument("--python", default="python3", help="python interpreter for the engine (venv)")
    ap.add_argument("--bavli-convention", action="store_true")
    ap.add_argument("--engine-fingerprint", default=None,
                    help="engine identity (commits/models/policy); a change forces a FULL relink")
    ap.add_argument("--engine-pool", action="store_true",
                    help="load the Sefaria library once and fork --engine-workers resolver "
                         "children from it (copy-on-write) instead of N independent loads")
    ap.add_argument("--engine-workers", type=int, default=1,
                    help="parallel link_books.py processes (claim-ledger coordinated)")
    ap.add_argument("--resume-checkpoints", action="store_true",
                    help="reuse exact-plan immutable batch shards and verified completed-book "
                         "outputs restored into --run-dir; claim/failed ledgers are rebuilt")
    ap.add_argument("--worker-stall-seconds", type=int, default=1800,
                    help="kill only a worker whose per-batch heartbeat is stale this long")
    ap.add_argument("--engine-restart-limit", type=int, default=2,
                    help="bounded replacements per nonzero-exiting engine worker")
    ap.add_argument("--forbid-full-relink", action="store_true",
                    help="serial mode: fail instead of a fingerprint-triggered full relink")
    ap.add_argument("--adopt-fingerprint", default=None, metavar="OLD::NEW",
                    help="operator-attested migration: re-stamp the baseline fingerprint "
                         "WITHOUT a full relink; both strings must match exactly")
    ap.add_argument("--ner-bundle-dir", default=None,
                    help="verified raw-NER bundle directory; resolve without a live GPU service")
    ap.add_argument("--plan-only", default=None, metavar="PATH",
                    help="write the immutable changed-book plan and exit without mutation")
    ap.add_argument("--relink-request-id", default=None,
                    help="64-hex correlation key embedded in --plan-only output")
    args = ap.parse_args()
    install_terminate_handler()
    if args.plan_only:
        n = write_incremental_plan(args, args.plan_only, args.relink_request_id)
        _log(f"plan complete: {n} books require NER")
        return
    if args.relink_request_id and not args.ner_bundle_dir:
        ap.error("--relink-request-id without --plan-only requires --ner-bundle-dir")
    summary = {}
    n = run_incremental(args, summary)
    _log(format_done_line(
        total=summary.get("total", n),
        adopted=summary.get("adopted", 0),
        computed=summary.get("computed", n),
        failed=summary.get("failed", 0),
        checkpoint_source=summary.get("checkpoint_source"),
    ))


if __name__ == "__main__":
    main()
