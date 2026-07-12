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
import sqlite3

sys_path = os.path.dirname(os.path.abspath(__file__))
import sys  # noqa: E402
sys.path.insert(0, sys_path)
from linker_artifact import BookKey, book_key_to_relpath, read_artifact, write_artifact  # noqa: E402


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
        os.remove(old_path)
    return True


def delete_source_artifact(repo: str, key: BookKey) -> bool:
    path = os.path.join(repo, book_key_to_relpath(key))
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def _with_book_key(rec, bk):
    from linker_artifact import LinkRecord
    return LinkRecord(
        book_key=bk, line_index=rec.line_index, start=rec.start, end=rec.end,
        target_ref=rec.target_ref, line_index_base=rec.line_index_base,
        source_path=rec.source_path, source_hash=rec.source_hash,
    )


# ── snapshot content hashing: the ONLY source-change clock ───────────────────

def snapshot_book_hashes(snapshot_db: str) -> dict[tuple[str, str], str]:
    """Per-book content hash over the snapshot's lines, keyed by (source_name, he_title).

    Streams rows ordered by book then line_index and folds each book's (line_index, content)
    into one sha1 — so any content or line change flips the hash, and it never materialises a
    whole book in memory. This is the exact content the linker links and stamps `source_hash`
    from, so a hash change here is precisely "this book must be re-linked"."""
    con = sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT source_name, canonical_he_title, line_index, content "
            "FROM lines_snapshot ORDER BY source_name, canonical_he_title, line_index"
        )
        hashes: dict[tuple[str, str], str] = {}
        cur_key = None
        h = None
        for s, t, li, content in rows:
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
        "schema_version": 2,
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


def run_incremental(args) -> int:
    """End-to-end incremental update. Returns the number of books re-linked."""
    repo = os.path.abspath(args.repo)
    baseline_dir = os.path.join(repo, "baseline")
    os.makedirs(baseline_dir, exist_ok=True)
    artifacts_dir = os.path.join(repo, "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)

    # 1. Diff the CURRENT snapshot's per-book content hashes against the baseline.
    #    A changed engine fingerprint invalidates the WHOLE baseline (full relink):
    #    the engine version shapes the output, so a partial relink under a new engine
    #    would leave the artifact store a mix of engines.
    current = snapshot_book_hashes(args.snapshot)
    baseline, stored_fingerprint = _read_baseline_file(baseline_dir)
    fingerprint = getattr(args, "engine_fingerprint", None)
    if baseline and fingerprint is not None and stored_fingerprint != fingerprint:
        _log("engine fingerprint changed "
             f"({stored_fingerprint!r} -> {fingerprint!r}) — FULL relink")
        baseline = {}
    changed, removed = plan_from_snapshot(current, baseline)
    _log(f"snapshot: {len(current)} books | changed/new={len(changed)} removed={len(removed)}")

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
    if changed:
        os.makedirs(args.run_dir, exist_ok=True)
        # Fresh engine ledger for THIS invocation. done/claim/failed markers are meaningful only
        # WITHIN one link_books.py run (poison-loop guard, worker claims, failure ledger). A stale
        # `done` from a reused run_dir would make the engine SKIP a changed book with no `failed`
        # marker → baseline would advance as if it linked → orphan. So we never depend on external
        # workspace cleanup: we clear them ourselves. (logs/ is append-only diagnostics — kept.)
        import shutil
        for d in ("done", "claim", "failed"):
            shutil.rmtree(os.path.join(args.run_dir, d), ignore_errors=True)
        only = os.path.join(args.run_dir, "changed_books.json")
        with open(only, "w", encoding="utf-8") as fh:
            json.dump([b.to_dict() for b in changed], fh, ensure_ascii=False)
        _log(f"re-linking {len(changed)} changed books")
        _run_engine(args, only)  # raises on whole-process failure → baseline NOT advanced below
        failed = read_failed_books(args.run_dir)

    # A per-book crash FAILS the whole run, loudly. In the serial pipeline the build waits
    # on this run and injects its output into the DB being released — a missing book would
    # ship a silently-incomplete link set (a NEW book leaves nothing for linkerStrict to
    # even hash-check). Baseline/meta are NOT advanced, so a rerun retries everything.
    if failed:
        raise RuntimeError(
            f"{len(failed)} book(s) failed inside the engine — failing the run "
            "(baseline not advanced): "
            + ", ".join(sorted(f"{s}/{t}" for s, t in failed)))

    # 5. Advance baseline + lineage. Any failure never reaches here (raised above), so the
    #    baseline advances to exactly the snapshot hashes that were fully linked.
    write_snapshot_baseline(baseline_dir, dict(current), engine_fingerprint=fingerprint)
    write_meta(
        repo,
        sefaria_export_tag=args.sefaria_tag,
        snapshot_sha256=sha256_of_file(args.snapshot),
        book_count=len(current),
        bavli_convention=args.bavli_convention,
        generated_at=args.generated_at,
        engine_fingerprint=fingerprint,
    )
    _log("baseline + meta.json updated")
    return len(changed)


def _run_engine(args, only_books_path):
    """Invoke link_books.py on the changed subset, in the Sefaria-Project venv/cwd."""
    import subprocess
    engine = os.path.join(os.path.dirname(os.path.abspath(__file__)), "link_books.py")
    cmd = [args.python, engine,
           "--snapshot", args.snapshot, "--repo", os.path.abspath(args.repo),
           "--run-dir", args.run_dir, "--only-books", only_books_path]
    if args.bavli_convention:
        cmd.append("--bavli-convention")
    env = dict(os.environ, PYTHONPATH=args.sef_project + ":" + os.environ.get("PYTHONPATH", ""))
    _log("running engine: " + " ".join(cmd))
    subprocess.run(cmd, cwd=args.sef_project, env=env, check=True)


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
    args = ap.parse_args()
    n = run_incremental(args)
    _log(f"done: {n} books re-linked")


if __name__ == "__main__":
    main()
