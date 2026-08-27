import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from incremental import restore_completed_local_checkpoint
from linker_artifact import BookKey, LinkRecord, book_key_to_relpath, content_hash, write_artifact


class CompletedCheckpointTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.run = self.root / "run"
        self.snapshot = self.root / "snapshot.db"
        self.book = BookKey("Source", "Book")
        self.claim = hashlib.sha1(b"Source\0Book").hexdigest()
        self.plan = [{
            "source_name": "Source",
            "canonical_he_title": "Book",
            "hash": "a" * 16,
            "ner_ranges": [[0, 1]],
            "reuse": [],
        }]
        con = sqlite3.connect(self.snapshot)
        con.execute(
            "CREATE TABLE lines_snapshot (source_name TEXT, canonical_he_title TEXT, "
            "line_index INTEGER, content TEXT, context_ref TEXT)"
        )
        con.execute(
            "INSERT INTO lines_snapshot VALUES (?,?,?,?,?)",
            ("Source", "Book", 0, "text here", "Source 1:1"),
        )
        con.commit()
        con.close()
        (self.run / "completed_artifacts").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def write_manifest(self, artifact):
        (self.run / "completed_books.json").write_text(json.dumps([{
            "claim_id": self.claim,
            "source_name": "Source",
            "canonical_he_title": "Book",
            "hash": "a" * 16,
            "artifact": artifact,
        }]), encoding="utf-8")

    def test_restores_semantically_verified_artifact_and_done_marker(self):
        cached = self.run / "completed_artifacts" / f"{self.claim}.jsonl"
        write_artifact(cached, [LinkRecord(
            book_key=self.book,
            line_index=0,
            start=0,
            end=4,
            target_ref="Genesis 1:1",
            source_hash=content_hash("text here"),
        )])
        self.write_manifest(cached.name)
        restored = restore_completed_local_checkpoint(
            repo=str(self.repo), snapshot=str(self.snapshot), run_dir=str(self.run),
            changed_books_payload=self.plan,
        )
        self.assertEqual(restored, 1)
        self.assertTrue((self.run / "done" / self.claim).is_file())
        self.assertTrue((self.repo / book_key_to_relpath(self.book)).is_file())

    def test_zero_link_completion_removes_stale_artifact(self):
        public = self.repo / book_key_to_relpath(self.book)
        public.parent.mkdir(parents=True)
        public.write_text("stale\n", encoding="utf-8")
        self.write_manifest(None)
        restore_completed_local_checkpoint(
            repo=str(self.repo), snapshot=str(self.snapshot), run_dir=str(self.run),
            changed_books_payload=self.plan,
        )
        self.assertFalse(public.exists())
        self.assertTrue((self.run / "done" / self.claim).is_file())

    def test_rejects_record_from_different_snapshot_content(self):
        cached = self.run / "completed_artifacts" / f"{self.claim}.jsonl"
        write_artifact(cached, [LinkRecord(
            book_key=self.book,
            line_index=0,
            start=0,
            end=4,
            target_ref="Genesis 1:1",
            source_hash=content_hash("different"),
        )])
        self.write_manifest(cached.name)
        with self.assertRaisesRegex(RuntimeError, "source hash differs"):
            restore_completed_local_checkpoint(
                repo=str(self.repo), snapshot=str(self.snapshot), run_dir=str(self.run),
                changed_books_payload=self.plan,
            )


if __name__ == "__main__":
    unittest.main()
