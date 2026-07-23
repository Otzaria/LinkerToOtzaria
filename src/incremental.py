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
    _log(f"snapshot: {len(current)} books | changed/new={len(changed)} removed={len(removed)}")
    return {
        "current": current,
        "changed": changed,
        "removed": removed,
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
        "schema_version": 1,
        "relink_request_id": relink_request_id,
        "snapshot_sha256": sha256_of_file(args.snapshot),
        "changelog_sha256": sha256_of_file(args.changelog) if args.changelog else None,
        "engine_fingerprint": plan["engine_fingerprint"],
        "changed": [book.to_dict() for book in plan["changed"]],
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


def run_incremental(args) -> int:
    """End-to-end incremental update. Returns the number of books re-linked."""
    repo = os.path.abspath(args.repo)
    baseline_dir = os.path.join(repo, "baseline")
    os.makedirs(baseline_dir, exist_ok=True)
    artifacts_dir = os.path.join(repo, "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)
    plan = compute_incremental_plan(args)
    current = plan["current"]
    changed = plan["changed"]
    removed = plan["removed"]
    fingerprint = plan["engine_fingerprint"]

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
    if changed:
        os.makedirs(args.run_dir, exist_ok=True)
        # Fresh engine ledger for THIS invocation. done/claim/failed markers are meaningful only
        # WITHIN one link_books.py run (poison-loop guard, worker claims, failure ledger). A stale
        # `done` from a reused run_dir would make the engine SKIP a changed book with no `failed`
        # marker → baseline would advance as if it linked → orphan. So we never depend on external
        # workspace cleanup: we clear them ourselves. (logs/ is append-only diagnostics — kept.)
        import shutil
        for d in ("done", "claim", "failed", "checkpoints"):
            shutil.rmtree(os.path.join(args.run_dir, d), ignore_errors=True)
        only = os.path.join(args.run_dir, "changed_books.json")
        with open(only, "w", encoding="utf-8") as fh:
            json.dump([
                {
                    **book.to_dict(),
                    "hash": current[(book.source_name, book.canonical_he_title)],
                }
                for book in changed
            ], fh, ensure_ascii=False)
        _log(f"re-linking {len(changed)} changed books")
        codes = _run_engine(args, only)
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
        raise RuntimeError(
            f"{len(failed)} book(s) failed inside the engine — failing the run "
            "(baseline not advanced"
            + (f"; worker exit codes {codes}" if any(codes) else "") + "): "
            + ", ".join(sorted(f"{s}/{t}" for s, t in failed)))

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


def _run_engine(args, only_books_path):
    """Invoke link_books.py on the changed subset, in the Sefaria-Project venv/cwd.

    --engine-workers N runs N engine processes in parallel — they coordinate through the
    run-dir claim ledger, so this is the same mechanism the bootstrap used. Returns the
    workers' exit codes; the caller judges them against the ledger (a dead worker whose
    books were finished by peers via stale-claim steal is not a failure).

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
    import threading
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
    _log(f"running engine ({workers} worker(s)): " + " ".join(base_cmd))
    lease_fds = ()
    try:
        os.fstat(9)
        lease_fds = (9,)
    except OSError:
        pass
    worker_labels = ["w1"] if workers == 1 else [f"w{n:02d}" for n in range(1, workers + 1)]
    labels = [["--label", label] for label in worker_labels]
    heartbeat_dir = os.path.join(os.path.abspath(args.run_dir), "worker-heartbeats")
    os.makedirs(heartbeat_dir, exist_ok=True)
    for label in worker_labels:
        try:
            os.remove(os.path.join(heartbeat_dir, label))
        except FileNotFoundError:
            pass
    procs = []
    scope_paths = []
    watchdog_stop = threading.Event()
    watchdog = None
    try:
        # Append immediately after every successful spawn.  A list comprehension
        # loses all already-created children if a later Popen raises before the
        # assignment completes, leaving an unowned worker/process group behind.
        for extra in labels:
            procs.append(subprocess.Popen(
                base_cmd + extra,
                cwd=args.sef_project,
                env=env,
                start_new_session=True,
                pass_fds=lease_fds,
            ))
        scope_dir = os.environ.get("LINKER_ENGINE_SCOPE_DIR")
        if scope_dir:
            os.makedirs(scope_dir, exist_ok=True)
            scope_tool = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ci", "process_scope.py")
            for p, label in zip(procs, worker_labels):
                state = os.path.join(scope_dir, f"engine-{label}.json")
                subprocess.run([
                    sys.executable, scope_tool, "record", "--state", state,
                    "--pid", str(p.pid), "--kind", f"linker-engine-{label}",
                    "--expect", "link_books.py",
                ], check=True)
                scope_paths.append((state, p))
        spawned_at = {p.pid: time.time() for p in procs}

        def monitor_heartbeats():
            stall = float(getattr(args, "worker_stall_seconds", 1800) or 1800)
            while not watchdog_stop.wait(min(30.0, max(1.0, stall / 10))):
                now = time.time()
                for p, label in zip(procs, worker_labels):
                    if p.poll() is not None:
                        continue
                    path = os.path.join(heartbeat_dir, label)
                    try:
                        last = max(spawned_at[p.pid], os.path.getmtime(path))
                    except OSError:
                        last = spawned_at[p.pid]
                    if now - last <= stall:
                        continue
                    _log(f"worker {label} pid={p.pid} has no heartbeat for {now-last:.0f}s; terminating its group")
                    try:
                        os.killpg(p.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        continue
                    if watchdog_stop.wait(10):
                        return
                    if p.poll() is None:
                        try:
                            os.killpg(p.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

        watchdog = threading.Thread(target=monitor_heartbeats, name="engine-heartbeat-watchdog", daemon=True)
        watchdog.start()
        codes = [p.wait() for p in procs]
    finally:
        watchdog_stop.set()
        if watchdog is not None:
            watchdog.join(timeout=2)
        live = [p for p in procs if p.poll() is None]
        for p in live:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.time() + float(os.environ.get("LINKER_PROCESS_TERM_GRACE", "15"))
        for p in live:
            while p.poll() is None and time.time() < deadline:
                time.sleep(0.5)
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        # Reap every child we created.  killpg() ending the process is not enough:
        # without wait(), the leader can remain a zombie and make ownership checks
        # report a false live group.
        for p in live:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for state, proc in scope_paths:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                try:
                    os.remove(state)
                except FileNotFoundError:
                    pass
            except PermissionError:
                _log(f"retaining process scope {state}: group {proc.pid} still exists but is not signalable")
            else:
                _log(f"retaining process scope {state}: group {proc.pid} still has live members")
        if live:
            _log(f"terminated {len(live)} live engine worker group(s) on exit")
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
    ap.add_argument("--engine-workers", type=int, default=1,
                    help="parallel link_books.py processes (claim-ledger coordinated)")
    ap.add_argument("--worker-stall-seconds", type=int, default=1800,
                    help="kill only a worker whose per-batch heartbeat is stale this long")
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
    n = run_incremental(args)
    _log(f"done: {n} books re-linked")


if __name__ == "__main__":
    main()
