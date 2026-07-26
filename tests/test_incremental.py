import os
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
    """Rows are v1 4-tuples or v2 tuples with a final context_ref."""
    import sqlite3
    con = sqlite3.connect(path)
    if rows and len(rows[0]) == 5:
        con.execute(
            "CREATE TABLE lines_snapshot(source_name TEXT, canonical_he_title TEXT, "
            "line_index INTEGER, content TEXT, context_ref TEXT)"
        )
        con.executemany("INSERT INTO lines_snapshot VALUES(?,?,?,?,?)", rows)
    else:
        con.execute(
            "CREATE TABLE lines_snapshot(source_name TEXT, canonical_he_title TEXT, "
            "line_index INTEGER, content TEXT)"
        )
        con.executemany("INSERT INTO lines_snapshot VALUES(?,?,?,?)", rows)
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

    def test_hash_changes_when_relative_citation_context_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "first.db")
            second = os.path.join(tmp, "second.db")
            _mk_snapshot(first, [
                ("Sefaria", "משנה ברכות", 4, "ראה לקמן", "משנה ברכות א, א"),
            ])
            _mk_snapshot(second, [
                ("Sefaria", "משנה ברכות", 4, "ראה לקמן", "משנה ברכות ב, א"),
            ])
            self.assertNotEqual(
                inc.snapshot_book_hashes(first),
                inc.snapshot_book_hashes(second),
            )

    def test_plan_from_snapshot_changed_and_removed(self):
        base = {("s", "a"): "h1", ("s", "b"): "h2", ("s", "gone"): "h3"}
        cur = {("s", "a"): "h1", ("s", "b"): "CHANGED", ("s", "new"): "h4"}
        changed, removed = inc.plan_from_snapshot(cur, base)
        self.assertEqual({(b.source_name, b.canonical_he_title) for b in changed}, {("s", "b"), ("s", "new")})
        self.assertEqual({(b.source_name, b.canonical_he_title) for b in removed}, {("s", "gone")})

    def test_baseline_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            hashes = {("Sefaria", "בראשית"): "aaaa", ("MoreBooks", "ספר"): "bbbb"}
            inc.write_snapshot_baseline(d, hashes)
            self.assertEqual(inc.read_snapshot_baseline(d), hashes)
            self.assertEqual(inc.read_snapshot_baseline(tempfile.gettempdir() + "/nope-xyz"), {})


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
