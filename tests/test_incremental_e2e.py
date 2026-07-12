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
    """rows: (source_name, canonical_he_title, line_index, content)."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE lines_snapshot(source_name TEXT, canonical_he_title TEXT, line_index INTEGER, content TEXT)")
    con.executemany("INSERT INTO lines_snapshot VALUES(?,?,?,?)", rows)
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
        self.fail_engine = False        # whole-process failure (engine raises)
        self.fail_books = set()         # per-book failures (engine writes a failed marker, exits 0)
        self._orig_engine = inc._run_engine
        inc._run_engine = self._fake_engine

    def tearDown(self):
        inc._run_engine = self._orig_engine

    def _fake_engine(self, args, only_books_path):
        import json
        with open(only_books_path, encoding="utf-8") as fh:
            req = {(b["source_name"], b["canonical_he_title"]) for b in json.load(fh)}
        self.requested.append(req)
        if self.fail_engine:
            raise RuntimeError("simulated engine failure")
        # mimic link_books.py: a per-book crash writes <run-dir>/failed/<any> = book_key, exits 0
        for bk in self.fail_books & req:
            fdir = os.path.join(args.run_dir, "failed")
            os.makedirs(fdir, exist_ok=True)
            with open(os.path.join(fdir, bk[1]), "w", encoding="utf-8") as ff:
                json.dump({"source_name": bk[0], "canonical_he_title": bk[1]}, ff, ensure_ascii=False)

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
