import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from linker_artifact import BookKey, LinkRecord, book_key_to_relpath, read_artifact, write_artifact  # noqa: E402
import incremental as inc  # noqa: E402


class RewriteTest(unittest.TestCase):
    def test_rewrite_basic_and_range_and_daf(self):
        self.assertEqual(inc.rewrite_target_ref("Psalms 16:8", "Psalms", "Tehillim"), "Tehillim 16:8")
        self.assertEqual(inc.rewrite_target_ref("Exodus 29:43-44", "Exodus", "Shemot"), "Shemot 29:43-44")
        self.assertEqual(inc.rewrite_target_ref("Shabbat 31a:6", "Shabbat", "Shabbos"), "Shabbos 31a:6")
        self.assertEqual(inc.rewrite_target_ref("Psalms", "Psalms", "Tehillim"), "Tehillim")

    def test_rewrite_avoids_prefix_trap(self):
        # "Genesis" must NOT rewrite "Genesis Rabbah 1:1" (next word is a letter, not a section).
        self.assertEqual(
            inc.rewrite_target_ref("Genesis Rabbah 1:1", "Genesis", "Bereshit"),
            "Genesis Rabbah 1:1",
        )
        # unrelated ref untouched
        self.assertEqual(inc.rewrite_target_ref("Numbers 7:89", "Psalms", "Tehillim"), "Numbers 7:89")

    def test_rewrite_multiword_title(self):
        self.assertEqual(
            inc.rewrite_target_ref("I Kings 8:11", "I Kings", "Melakhim I"),
            "Melakhim I 8:11",
        )


def _mk_snapshot(path, rows):
    """Rows are 4-tuples; context defaults to the canonical book title."""
    import sqlite3
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


class SnapshotHashTest(unittest.TestCase):
    def test_hash_is_per_book_and_content_sensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "s.db")
            _mk_snapshot(p, [
                ("Sefaria", "בראשית", 0, "א"), ("Sefaria", "בראשית", 1, "ב"),
                ("MoreBooks", "ספר", 0, "x"),
            ])
            h1 = inc.snapshot_book_hashes(p)
            self.assertEqual(set(h1), {("Sefaria", "בראשית"), ("MoreBooks", "ספר")})
            # change one line of one book -> only that book's hash flips
            p2 = os.path.join(tmp, "s2.db")
            _mk_snapshot(p2, [
                ("Sefaria", "בראשית", 0, "א"), ("Sefaria", "בראשית", 1, "CHANGED"),
                ("MoreBooks", "ספר", 0, "x"),
            ])
            h2 = inc.snapshot_book_hashes(p2)
            self.assertNotEqual(h1[("Sefaria", "בראשית")], h2[("Sefaria", "בראשית")])
            self.assertEqual(h1[("MoreBooks", "ספר")], h2[("MoreBooks", "ספר")])

    def test_plan_from_snapshot_changed_and_removed(self):
        base = {("s", "a"): "h1", ("s", "b"): "h2", ("s", "gone"): "h3"}
        cur = {("s", "a"): "h1", ("s", "b"): "CHANGED", ("s", "new"): "h4"}
        changed, removed = inc.plan_from_snapshot(cur, base)
        self.assertEqual({(b.source_name, b.canonical_he_title) for b in changed}, {("s", "b"), ("s", "new")})
        self.assertEqual({(b.source_name, b.canonical_he_title) for b in removed}, {("s", "gone")})

    def test_hash_changes_when_relative_context_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "first.db")
            second = os.path.join(tmp, "second.db")
            for path, context in ((first, "ברכות א, א"), (second, "ברכות ב, א")):
                con = sqlite3.connect(path)
                con.execute(
                    "CREATE TABLE lines_snapshot(source_name TEXT, canonical_he_title TEXT, "
                    "line_index INTEGER, content TEXT, context_ref TEXT)"
                )
                con.execute("INSERT INTO lines_snapshot VALUES(?,?,?,?,?)", ("Sefaria", "ברכות", 4, "ראה לקמן", context))
                con.commit()
                con.close()
            self.assertNotEqual(inc.snapshot_book_hashes(first), inc.snapshot_book_hashes(second))

    def test_baseline_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            hashes = {("Sefaria", "בראשית"): "aaaa", ("MoreBooks", "ספר"): "bbbb"}
            inc.write_snapshot_baseline(d, hashes)
            self.assertEqual(inc.read_snapshot_baseline(d), hashes)
            self.assertEqual(inc.read_snapshot_baseline(tempfile.gettempdir() + "/nope-xyz"), {})


class ProgressFormatTest(unittest.TestCase):
    """The resolve stage's only liveness signal, so it must be readable and honest."""

    def test_reports_books_lines_elapsed_and_eta(self):
        line = inc.format_progress(
            books_done=1234, books_total=5127,
            lines_done=380_000, lines_total=1_558_785,
            elapsed=5025.0, workers_alive=12, workers_replaced=3,
        )
        self.assertEqual(
            line,
            # 1234 books in 1:23:45 → 3893 left at that rate → 4h24m, stated as h:mm.
            "progress: books 1234/5127 (24.1%) · lines 380k/1.56M · elapsed 1:23:45 "
            "· eta ~4:24 · workers 12 alive, 3 replaced",
        )

    def test_first_interval_says_eta_not_a_number(self):
        # No book is done yet: a rate cannot be measured, so no rate is invented.
        line = inc.format_progress(
            books_done=0, books_total=10, elapsed=60.0,
            workers_alive=2, workers_replaced=0,
        )
        self.assertIn("books 0/10 (0.0%)", line)
        self.assertIn("eta n/a", line)
        self.assertNotIn("lines", line)  # no line plan → the axis is omitted, not "0/0"

    def test_completion_is_exactly_one_hundred_percent(self):
        line = inc.format_progress(
            books_done=7, books_total=7, lines_done=42, lines_total=42,
            elapsed=3.0, workers_alive=0, workers_replaced=1,
        )
        self.assertIn("books 7/7 (100.0%)", line)
        self.assertIn("lines 42/42", line)
        self.assertIn("eta ~0:00", line)

    def test_empty_plan_does_not_divide_by_zero(self):
        self.assertIn(
            "books 0/0 (0.0%)",
            inc.format_progress(books_done=0, books_total=0, elapsed=0.0,
                                workers_alive=0, workers_replaced=0),
        )

    def test_adopted_books_are_named_and_kept_out_of_the_eta_rate(self):
        # A recovery adopts 3900 books before the engine starts.  100 more were computed
        # in the first 10 minutes, so the remaining 1127 need ~1:52 — NOT the ~0:01 that
        # dividing by all 4000 "done" books would predict.
        line = inc.format_progress(
            books_done=4000, books_total=5127, books_adopted=3900,
            elapsed=600.0, workers_alive=12, workers_replaced=0,
        )
        self.assertIn("books 4000/5127 (78.0%, 3900 adopted)", line)
        self.assertIn("eta ~1:52", line)

    def test_adopting_everything_reports_no_rate_instead_of_a_fake_one(self):
        # Nothing has been computed yet: the only completed books came from the
        # checkpoint, so there is no rate to measure and none is invented.
        line = inc.format_progress(
            books_done=5126, books_total=5127, books_adopted=5126,
            elapsed=60.0, workers_alive=12, workers_replaced=0,
        )
        self.assertIn("books 5126/5127 (100.0%, 5126 adopted)", line)
        self.assertIn("eta n/a", line)


    def test_recycles_for_heavy_books_are_named_only_when_they_happened(self):
        # The heavy phase forks a fresh worker per deferred book (L5); without this
        # clause an operator reads those forks as instability in `N replaced`.
        self.assertIn(
            "workers 12 alive, 3 replaced, 12 recycled-for-heavy",
            inc.format_progress(
                books_done=5120, books_total=5127, elapsed=6000.0,
                workers_alive=12, workers_replaced=3, workers_recycled_for_heavy=12,
            ),
        )
        # A run with no deferred book must not carry a permanent `0` clause.
        self.assertNotIn(
            "recycled-for-heavy",
            inc.format_progress(books_done=1, books_total=2, elapsed=1.0,
                                workers_alive=1, workers_replaced=0),
        )


class MemorySummaryTest(unittest.TestCase):
    """`memory:` reports the MemoryError requeues — and only when there were some."""

    def test_nothing_is_printed_when_no_book_ran_out_of_address_space(self):
        self.assertIsNone(inc.format_memory_summary(requeued=0, succeeded=0, failed=0))

    def test_one_requeued_book_that_completed(self):
        # What relink 34016397157 would have printed instead of failing on book 5127.
        self.assertEqual(
            inc.format_memory_summary(requeued=1, succeeded=1, failed=0),
            "memory: 1 book requeued after MemoryError, 1 succeeded on retry, 0 failed",
        )

    def test_several_requeued_books_one_of_which_failed_again(self):
        self.assertEqual(
            inc.format_memory_summary(requeued=3, succeeded=2, failed=1),
            "memory: 3 books requeued after MemoryError, 2 succeeded on retry, 1 failed",
        )


class FailedBookNotesTest(unittest.TestCase):
    """The engine's own explanation reaches the driver's ##[error], or nothing does."""

    def test_note_is_read_back_per_book_and_absence_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as run:
            self.assertEqual(inc.read_failed_notes(run), {})
            failed = os.path.join(run, "failed")
            os.makedirs(failed)
            import json as _json
            with open(os.path.join(failed, "a" * 40), "w", encoding="utf-8") as fh:
                _json.dump({"source_name": "Sefaria", "canonical_he_title": "ספר",
                            "note": "MemoryError twice, the second time on a fresh worker"},
                           fh, ensure_ascii=False)
            # A pre-L5 failure marker carries no note; it must still parse.
            with open(os.path.join(failed, "b" * 40), "w", encoding="utf-8") as fh:
                _json.dump({"source_name": "Sefaria", "canonical_he_title": "אחר"},
                           fh, ensure_ascii=False)
            notes = inc.read_failed_notes(run)
            self.assertEqual(list(notes), [("Sefaria", "ספר")])
            self.assertIn("fresh worker", notes[("Sefaria", "ספר")])
            # read_failed_books still sees both books: the note is extra, not a schema.
            self.assertEqual(
                inc.read_failed_books(run),
                {("Sefaria", "ספר"), ("Sefaria", "אחר")},
            )


class DoneLineTest(unittest.TestCase):
    """`done:` must never present adopted books as books this run linked."""

    def test_adopted_books_are_named_separately_from_computed(self):
        # Recovery 34021656701: 5126 books came from a checkpoint, 1 was computed.
        self.assertEqual(
            inc.format_done_line(total=5127, adopted=5126, computed=1, failed=0,
                                 checkpoint_source="source-34016397157-1"),
            "done: 5127 books (adopted 5126 from checkpoint source-34016397157-1, "
            "computed 1, failed 0)",
        )

    def test_run_without_a_checkpoint_claims_only_computed_books(self):
        self.assertEqual(
            inc.format_done_line(total=3, adopted=0, computed=3, failed=0),
            "done: 3 books (computed 3, failed 0)",
        )

    def test_adoption_without_a_named_source_still_admits_the_adoption(self):
        self.assertEqual(
            inc.format_done_line(total=4, adopted=4, computed=0, failed=0),
            "done: 4 books (adopted 4 from checkpoint local checkpoint, computed 0, failed 0)",
        )


class CheckpointSourceTest(unittest.TestCase):
    def test_reads_the_stamp_and_tolerates_its_absence(self):
        with tempfile.TemporaryDirectory() as run:
            self.assertIsNone(inc.read_checkpoint_source(run))
            with open(os.path.join(run, "checkpoint_source.txt"), "w", encoding="utf-8") as fh:
                fh.write("source-34016397157-1\n")
            self.assertEqual(inc.read_checkpoint_source(run), "source-34016397157-1")


class ArtifactMutationTest(unittest.TestCase):
    def _mk(self, repo, bk, refs, source_hash=None):
        recs = [LinkRecord(bk, i, 0, 3, r, source_hash=source_hash) for i, r in enumerate(refs)]
        write_artifact(os.path.join(repo, book_key_to_relpath(bk)), recs)

    def test_source_hash_survives_rewrite_and_relocate(self):
        # The source-drift guard is useless if a mutated record loses its source_hash — the
        # Kotlin importer only checks when the field is present. Both copy helpers must carry it.
        h = "0123456789abcdef"
        with tempfile.TemporaryDirectory() as repo:
            self._mk(repo, BookKey("MoreBooks", "ספר"), ["Psalms 16:8"], source_hash=h)
            inc.apply_en_renames(os.path.join(repo, "artifacts"), [{"old_en": "Psalms", "new_en": "Tehillim"}])
            recs = list(read_artifact(os.path.join(repo, book_key_to_relpath(BookKey("MoreBooks", "ספר")))))
            self.assertEqual(recs[0].target_ref, "Tehillim 16:8")
            self.assertEqual(recs[0].source_hash, h)  # rewrite kept the guard
        with tempfile.TemporaryDirectory() as repo:
            old, new = BookKey("Sefaria", "ישעיה"), BookKey("Sefaria", "ישעיהו")
            self._mk(repo, old, ["Genesis 1:1"], source_hash=h)
            inc.relocate_source_artifact(repo, old, new)
            recs = list(read_artifact(os.path.join(repo, book_key_to_relpath(new))))
            self.assertEqual(recs[0].source_hash, h)  # relocate kept the guard

    def test_apply_en_renames(self):
        with tempfile.TemporaryDirectory() as repo:
            self._mk(repo, BookKey("MoreBooks", "ספר"), ["Psalms 16:8", "Genesis Rabbah 1:1", "Exodus 2:2"])
            n = inc.apply_en_renames(os.path.join(repo, "artifacts"),
                                     [{"old_en": "Psalms", "new_en": "Tehillim"}])
            self.assertEqual(n, 1)
            refs = [r.target_ref for r in read_artifact(os.path.join(repo, book_key_to_relpath(BookKey("MoreBooks", "ספר"))))]
            self.assertEqual(refs, ["Tehillim 16:8", "Genesis Rabbah 1:1", "Exodus 2:2"])

    def test_relocate_and_delete(self):
        with tempfile.TemporaryDirectory() as repo:
            old = BookKey("Sefaria", "ישעיה")
            new = BookKey("Sefaria", "ישעיהו")
            self._mk(repo, old, ["Genesis 1:1"])
            self.assertTrue(inc.relocate_source_artifact(repo, old, new))
            self.assertFalse(os.path.exists(os.path.join(repo, book_key_to_relpath(old))))
            recs = list(read_artifact(os.path.join(repo, book_key_to_relpath(new))))
            self.assertEqual(recs[0].book_key, new)  # embedded key rewritten
            self.assertTrue(inc.delete_source_artifact(repo, new))
            self.assertFalse(os.path.exists(os.path.join(repo, book_key_to_relpath(new))))


if __name__ == "__main__":
    unittest.main()
