import argparse
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from linker_artifact import BookKey, LinkRecord, book_key_to_relpath, write_artifact  # noqa: E402
import incremental as inc  # noqa: E402


def _snapshot(path, rows):
    """Rows are 4-tuples; context defaults to the canonical book title."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE lines_snapshot(source_name TEXT, canonical_he_title TEXT, "
        "line_index INTEGER, content TEXT, context_ref TEXT)"
    )
    con.executemany("INSERT INTO lines_snapshot VALUES(?,?,?,?,?)", [
        (*row, row[1]) for row in rows
    ])
    con.commit()
    con.close()


class IncrementalE2ETest(unittest.TestCase):
    """The source-change clock is the snapshot, not upstream manifests. These tests pin the
    systemic fix for the cross-cycle bug: a book is re-linked EXACTLY when its snapshot
    content changes, the baseline advances only for content actually linked, and a failed
    engine run never advances the baseline (so a book can't be orphaned)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "baseline"))
        os.makedirs(os.path.join(self.repo, "artifacts"))
        self.run_dir = os.path.join(self.tmp, "run")
        os.makedirs(self.run_dir)
        self.snap = os.path.join(self.tmp, "snap.db")
        # capture what the engine is asked to link, instead of really linking
        self.requested = []
        self.requested_payloads = []
        self.fail_engine = False        # whole-process failure (engine raises)
        self.fail_books = set()         # per-book failures (engine writes a failed marker, exits 0)
        self.skip_done_books = set()    # books left with NO marker at all (the outage gap)
        self.engine_exit_codes = [0]    # per-worker exit codes returned by the engine
        self._orig_engine = inc._run_engine
        inc._run_engine = self._fake_engine

    def tearDown(self):
        inc._run_engine = self._orig_engine

    def _fake_engine(self, args, only_books_path):
        import json
        from link_books import claim_id
        from linker_artifact import BookKey
        with open(only_books_path, encoding="utf-8") as fh:
            payload = json.load(fh)
            req = {(b["source_name"], b["canonical_he_title"]) for b in payload}
        self.requested_payloads.append(payload)
        self.requested.append(req)
        if self.fail_engine:
            raise RuntimeError("simulated engine failure")
        # mimic link_books.py: every processed book gets a done marker; a per-book crash
        # ALSO writes <run-dir>/failed/<any> = book_key. skip_done_books simulates the
        # no-marker bug (a book every worker walked past during a NER outage).
        done_dir = os.path.join(args.run_dir, "done")
        os.makedirs(done_dir, exist_ok=True)
        for bk in req:
            if bk in getattr(self, "skip_done_books", set()):
                continue
            open(os.path.join(done_dir, claim_id(BookKey(*bk))), "w").close()
        for bk in self.fail_books & req:
            fdir = os.path.join(args.run_dir, "failed")
            os.makedirs(fdir, exist_ok=True)
            with open(os.path.join(fdir, bk[1]), "w", encoding="utf-8") as ff:
                json.dump({"source_name": bk[0], "canonical_he_title": bk[1]}, ff, ensure_ascii=False)
        # mimic _run_engine's contract: per-worker exit codes, judged against the ledger
        return self.engine_exit_codes

    def _args(self):
        return argparse.Namespace(
            repo=self.repo, snapshot=self.snap, changelog=None,
            sefaria_tag="20260101000000", generated_at="2026-01-01T00:00:00Z",
            run_dir=self.run_dir, sef_project=self.tmp, python="python3", bavli_convention=False,
        )

    A = ("Sefaria", "בראשית")
    B = ("Sefaria", "שמות")
    C = ("DictaToOtzaria", "אור הישר")

    def _rows(self, a="a1", b="b1", c="c1", drop=()):  # per-book content
        rows = []
        for (sn, ct), val in ((self.A, a), (self.B, b), (self.C, c)):
            if (sn, ct) in drop:
                continue
            rows.append((sn, ct, 0, val))
        return rows

    def test_relink_tracks_snapshot_and_never_orphans(self):
        import json
        # run1: baseline empty -> every book is "new" -> relink all 3.
        _snapshot(self.snap, self._rows())
        self.assertEqual(inc.run_incremental(self._args()), 3)
        self.assertEqual(self.requested[-1], {self.A, self.B, self.C})
        with open(os.path.join(self.repo, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertEqual(meta["sefaria"]["export_tag"], "20260101000000")
        self.assertEqual(meta["snapshot"]["book_count"], 3)

        # run2: identical snapshot -> nothing changed.
        self.assertEqual(inc.run_incremental(self._args()), 0)

        # run3: only book A's content changes in the snapshot -> only A re-linked.
        os.remove(self.snap)
        _snapshot(self.snap, self._rows(a="a2"))
        self.assertEqual(inc.run_incremental(self._args()), 1)
        self.assertEqual(self.requested[-1], {self.A})

        # run4: same v2 snapshot -> baseline advanced only for what was linked -> 0.
        self.assertEqual(inc.run_incremental(self._args()), 0)

    def test_exact_plan_checkpoint_resume_preserves_shards_but_rebuilds_ledgers(self):
        _snapshot(self.snap, self._rows())
        self.fail_engine = True
        with self.assertRaisesRegex(RuntimeError, "simulated engine failure"):
            inc.run_incremental(self._args())
        checkpoint = os.path.join(self.run_dir, "checkpoints", "sentinel")
        os.makedirs(os.path.dirname(checkpoint), exist_ok=True)
        with open(checkpoint, "w", encoding="utf-8") as stream:
            stream.write("immutable")
        os.makedirs(os.path.join(self.run_dir, "done"), exist_ok=True)
        open(os.path.join(self.run_dir, "done", "stale"), "w").close()

        self.fail_engine = False
        args = self._args()
        args.resume_checkpoints = True
        self.assertEqual(inc.run_incremental(args), 3)
        self.assertTrue(os.path.isfile(checkpoint))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "done", "stale")))

    def test_checkpoint_resume_rejects_a_different_plan(self):
        _snapshot(self.snap, self._rows())
        self.fail_engine = True
        with self.assertRaisesRegex(RuntimeError, "simulated engine failure"):
            inc.run_incremental(self._args())
        only = os.path.join(self.run_dir, "changed_books.json")
        with open(only, "w", encoding="utf-8") as stream:
            stream.write("[]")
        os.makedirs(os.path.join(self.run_dir, "checkpoints"), exist_ok=True)
        args = self._args()
        args.resume_checkpoints = True
        with self.assertRaisesRegex(RuntimeError, "differs from the exact current"):
            inc.run_incremental(args)

    def test_changed_book_sends_only_unmatched_lines_to_ner(self):
        _snapshot(self.snap, [
            (*self.A, 0, "א"),
            (*self.A, 1, "ב"),
            (*self.A, 2, "ג"),
        ])
        self.assertEqual(inc.run_incremental(self._args()), 1)
        os.remove(self.snap)
        _snapshot(self.snap, [
            (*self.A, 0, "א"),
            (*self.A, 1, "חדש"),
            (*self.A, 2, "ג"),
        ])
        self.assertEqual(inc.run_incremental(self._args()), 1)
        item = self.requested_payloads[-1][0]
        self.assertEqual(item["ner_ranges"], [[1, 2]])
        self.assertEqual(item["reuse"], [[0, 0], [2, 2]])

    def test_line_reuse_refuses_unexpected_prior_artifact(self):
        _snapshot(self.snap, [
            (*self.A, 0, "א"),
            (*self.A, 1, "ב"),
        ])
        inc.run_incremental(self._args())
        # The fake engine emitted no artifact, so the accepted line baseline records
        # that absence. Injecting a file afterwards must not be treated as a valid store.
        artifact = os.path.join(self.repo, book_key_to_relpath(BookKey(*self.A)))
        write_artifact(artifact, [
            LinkRecord(
                BookKey(*self.A), 0, 0, 1, "Genesis 1:1",
                source_hash="0" * 16,
            )
        ])
        os.remove(self.snap)
        _snapshot(self.snap, [
            (*self.A, 0, "א"),
            (*self.A, 1, "חדש"),
        ])
        with self.assertRaisesRegex(RuntimeError, "expected no prior artifact"):
            inc.run_incremental(self._args())

    def test_failed_engine_does_not_advance_baseline(self):
        # A book linked against a snapshot must NOT be marked done unless the run succeeded —
        # otherwise it would be orphaned (never retried). Simulate a crash, then a clean retry.
        _snapshot(self.snap, self._rows())
        self.fail_engine = True
        with self.assertRaises(RuntimeError):
            inc.run_incremental(self._args())
        # baseline was NOT written -> the books are still all "new" on the next run.
        self.assertEqual(inc.read_snapshot_baseline(os.path.join(self.repo, "baseline")), {})
        self.fail_engine = False
        self.assertEqual(inc.run_incremental(self._args()), 3)
        self.assertEqual(self.requested[-1], {self.A, self.B, self.C})

    def test_per_book_failure_fails_the_run_loudly(self):
        # A book that crashes INSIDE the engine (process still exits 0) must FAIL the whole
        # run: in the serial pipeline the build waits on this output, and a missing book
        # would ship a silently-incomplete DB. Baseline untouched → the rerun retries all.
        _snapshot(self.snap, self._rows())
        self.fail_books = {self.B}
        with self.assertRaises(RuntimeError) as ctx:
            inc.run_incremental(self._args())
        self.assertIn("שמות", str(ctx.exception))
        self.assertEqual(inc.read_snapshot_baseline(os.path.join(self.repo, "baseline")), {})

        # rerun with a healthy engine: everything is still "new" → all 3 linked, baseline full.
        self.fail_books = set()
        self.assertEqual(inc.run_incremental(self._args()), 3)
        self.assertEqual(self.requested[-1], {self.A, self.B, self.C})
        base = inc.read_snapshot_baseline(os.path.join(self.repo, "baseline"))
        self.assertEqual(set(base), {self.A, self.B, self.C})

    def test_engine_fingerprint_change_forces_full_relink(self):
        # Same snapshot, new engine fingerprint → the whole baseline is invalidated so the
        # artifact store is never a mix of two engine versions.
        _snapshot(self.snap, self._rows())
        args = self._args()
        args.engine_fingerprint = "engine-v1"
        self.assertEqual(inc.run_incremental(args), 3)
        self.assertEqual(inc.run_incremental(args), 0)  # same engine → delta empty
        args.engine_fingerprint = "engine-v2"
        self.assertEqual(inc.run_incremental(args), 3)  # new engine → full relink
        self.assertEqual(self.requested[-1], {self.A, self.B, self.C})
        self.assertEqual(
            inc.read_baseline_fingerprint(os.path.join(self.repo, "baseline")),
            "engine-v2")

    def test_fingerprint_full_relink_still_deletes_removed_books(self):
        # A book that left the snapshot in the SAME cycle as an engine change must still
        # have its artifact deleted — `removed` is planned against the ORIGINAL baseline.
        _snapshot(self.snap, self._rows())
        args = self._args()
        args.engine_fingerprint = "engine-v1"
        inc.run_incremental(args)
        art = os.path.join(self.repo, "artifacts", self.B[0], f"{self.B[1]}.jsonl")
        os.makedirs(os.path.dirname(art), exist_ok=True)
        open(art, "w").close()
        os.remove(self.snap)
        _snapshot(self.snap, self._rows(drop={self.B}))  # B gone from the snapshot
        args.engine_fingerprint = "engine-v2"            # + engine changed
        inc.run_incremental(args)
        self.assertFalse(os.path.exists(art), "removed book's artifact must be deleted")
        self.assertEqual(self.requested[-1], {self.A, self.C})

    def test_book_with_no_marker_fails_the_run(self):
        # A worker can exit 0 leaving a book with NEITHER done NOR failed (e.g. its
        # claim was held during a NER outage while every worker walked past it).
        # The driver must refuse to advance the baseline over such a gap.
        _snapshot(self.snap, self._rows())
        self.skip_done_books = {self.B}
        with self.assertRaises(RuntimeError) as ctx:
            inc.run_incremental(self._args())
        self.assertIn("neither done nor failed", str(ctx.exception))
        self.assertEqual(inc.read_snapshot_baseline(os.path.join(self.repo, "baseline")), {})

        self.skip_done_books = set()
        self.assertEqual(inc.run_incremental(self._args()), 3)  # clean rerun settles all

    def test_dead_worker_with_complete_ledger_proceeds(self):
        # A worker killed mid-run (kernel OOM on a huge book) whose books were finished
        # by peers via stale-claim steal leaves a complete ledger — the run must NOT be
        # failed over the corpse, or every such death forces a from-zero multi-hour rerun.
        _snapshot(self.snap, self._rows())
        self.engine_exit_codes = [0, -9, 0]
        self.assertEqual(inc.run_incremental(self._args()), 3)
        self.assertEqual(len(inc.read_snapshot_baseline(os.path.join(self.repo, "baseline"))), 3)

    def test_dead_worker_with_incomplete_ledger_fails(self):
        # Same death, but a book was left with no marker — the hole is fatal and the
        # error must carry the exit codes so the death is visible in the failure.
        _snapshot(self.snap, self._rows())
        self.engine_exit_codes = [0, -9, 0]
        self.skip_done_books = {self.B}
        with self.assertRaises(RuntimeError) as ctx:
            inc.run_incremental(self._args())
        self.assertIn("neither done nor failed", str(ctx.exception))
        self.assertIn("exit codes", str(ctx.exception))
        self.assertEqual(inc.read_snapshot_baseline(os.path.join(self.repo, "baseline")), {})

    def test_serial_mode_forbids_fingerprint_full_relink(self):
        # Under a waiting build (serial), a real fingerprint change must fail fast with
        # instructions instead of starting an ~11h full relink; adoption stays allowed.
        _snapshot(self.snap, self._rows())
        args = self._args()
        args.forbid_full_relink = True
        args.engine_fingerprint = "engine-v1"   # v1→adopt path: allowed in serial
        self.assertEqual(inc.run_incremental(args), 3)
        args.engine_fingerprint = "engine-v2"
        with self.assertRaises(RuntimeError) as ctx:
            inc.run_incremental(args)
        self.assertIn("standalone", str(ctx.exception))
        self.assertEqual(
            inc.read_baseline_fingerprint(os.path.join(self.repo, "baseline")),
            "engine-v1")  # baseline untouched

    def test_v1_baseline_adopts_fingerprint_without_full_relink(self):
        # One-time migration: a bootstrap-era baseline (no fingerprint) adopts the current
        # fingerprint instead of triggering an unplanned 7,332-book full relink mid-build.
        _snapshot(self.snap, self._rows())
        args = self._args()
        inc.run_incremental(args)  # fingerprint None → v2 file with fingerprint null
        args.engine_fingerprint = "engine-v1"
        self.assertEqual(inc.run_incremental(args), 0)   # ADOPT, not full relink
        self.assertEqual(
            inc.read_baseline_fingerprint(os.path.join(self.repo, "baseline")),
            "engine-v1")
        args.engine_fingerprint = "engine-v2"
        self.assertEqual(inc.run_incremental(args), 3)   # real change still full-relinks

    def test_operator_attested_adoption_restamps_without_relink(self):
        # An output-neutral engine change (failure handling / logging) may be migrated
        # by explicit operator attestation instead of a multi-day full relink — both
        # fingerprints must be pasted exactly; any drift fails loudly.
        _snapshot(self.snap, self._rows())
        args = self._args()
        args.engine_fingerprint = "engine-v1"
        inc.run_incremental(args)                        # baseline stamped engine-v1

        args.engine_fingerprint = "engine-v2"
        args.adopt_fingerprint = "engine-WRONG::engine-v2"
        with self.assertRaises(RuntimeError) as ctx:     # stale attestation → refuse
            inc.run_incremental(args)
        self.assertIn("attestation mismatch", str(ctx.exception))

        args.adopt_fingerprint = "engine-v1::engine-v2"
        self.assertEqual(inc.run_incremental(args), 0)   # re-stamp, no relink
        self.assertEqual(
            inc.read_baseline_fingerprint(os.path.join(self.repo, "baseline")),
            "engine-v2")
        args.adopt_fingerprint = None
        self.assertEqual(inc.run_incremental(args), 0)   # now a clean no-op

    def test_changelog_books_en_renamed_contract(self):
        # The real changelog_diff.json nests the diff under "books" (see SefariaExport
        # generate_changelog.py) — the driver must read it from there, not the root.
        import json as _json
        _snapshot(self.snap, self._rows())
        self.assertEqual(inc.run_incremental(self._args()), 3)
        art = os.path.join(self.repo, "artifacts", "Sefaria", "בראשית.jsonl")
        os.makedirs(os.path.dirname(art), exist_ok=True)
        from linker_artifact import BookKey, LinkRecord, write_artifact
        write_artifact(art, [LinkRecord(
            book_key=BookKey("Sefaria", "בראשית"), line_index=0, start=0, end=4,
            target_ref="Old Name 1:1", source_hash="0" * 16)])
        changelog = os.path.join(self.tmp, "changelog_diff.json")
        with open(changelog, "w", encoding="utf-8") as fh:
            _json.dump({"new_tag": "t2", "old_tag": "t1",
                        "books": {"en_renamed": [
                            {"old_en": "Old Name", "new_en": "New Name"}]},
                        "versions": {"added": []}}, fh, ensure_ascii=False)
        args = self._args()
        args.changelog = changelog
        inc.run_incremental(args)
        from linker_artifact import read_artifact
        recs = list(read_artifact(art))
        self.assertEqual(recs[0].target_ref, "New Name 1:1")

    def test_stale_run_dir_ledger_is_cleared_before_engine(self):
        # A reused run_dir must not let a stale `done` marker skip a changed book (which would
        # advance the baseline with no `failed` marker → orphan). The driver resets the ledger
        # itself, so correctness never depends on the workflow's external workspace cleanup.
        _snapshot(self.snap, self._rows())
        for d in ("done", "claim", "failed"):
            os.makedirs(os.path.join(self.run_dir, d), exist_ok=True)
            with open(os.path.join(self.run_dir, d, "stale"), "w", encoding="utf-8") as fh:
                fh.write("x")
        inc.run_incremental(self._args())
        for d in ("done", "claim", "failed"):
            self.assertFalse(os.path.exists(os.path.join(self.run_dir, d, "stale")))

    def test_book_removed_from_snapshot_deletes_its_artifact(self):
        # seed baseline + an artifact for C, then drop C from the snapshot.
        _snapshot(self.snap, self._rows())
        inc.run_incremental(self._args())
        c_path = os.path.join(self.repo, book_key_to_relpath(BookKey(*self.C)))
        write_artifact(c_path, [LinkRecord(BookKey(*self.C), 0, 0, 3, "Genesis 1:1", source_hash="0" * 16)])
        self.assertTrue(os.path.exists(c_path))
        os.remove(self.snap)
        _snapshot(self.snap, self._rows(drop=(self.C,)))
        inc.run_incremental(self._args())
        self.assertFalse(os.path.exists(c_path))  # artifact for the departed book is gone
        self.assertNotIn(self.C, inc.read_snapshot_baseline(os.path.join(self.repo, "baseline")))


if __name__ == "__main__":
    unittest.main()
