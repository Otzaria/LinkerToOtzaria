import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "ci"))
sys.path.insert(0, str(ROOT / "src"))

from incremental import sha256_of_file, snapshot_book_hashes, write_snapshot_baseline  # noqa: E402
from linker_artifact import (  # noqa: E402
    BookKey,
    LinkRecord,
    book_key_to_relpath,
    content_hash,
    write_artifact,
)
from seed_line_baseline import seed  # noqa: E402


class SeedLineBaselineTest(unittest.TestCase):
    FINGERPRINT = "alpha=1;omega=2;policy=drop;bavli=0"

    def _fixture(self, root: Path):
        repo = root / "repo"
        (repo / "baseline").mkdir(parents=True)
        (repo / "artifacts").mkdir()
        snapshot = root / "snapshot.db"
        connection = sqlite3.connect(snapshot)
        connection.execute(
            "CREATE TABLE lines_snapshot("
            "source_name TEXT, canonical_he_title TEXT, line_index INTEGER, content TEXT)"
        )
        connection.execute(
            "INSERT INTO lines_snapshot VALUES(?,?,?,?)",
            ("Source", "ספר", 0, "תוכן"),
        )
        connection.commit()
        connection.close()
        hashes = snapshot_book_hashes(str(snapshot))
        write_snapshot_baseline(
            str(repo / "baseline"),
            hashes,
            engine_fingerprint=self.FINGERPRINT,
        )
        (repo / "meta.json").write_text(
            json.dumps({
                "snapshot": {
                    "sha256": sha256_of_file(str(snapshot)),
                    "book_count": 1,
                },
                "engine": {"fingerprint": self.FINGERPRINT},
            }),
            encoding="utf-8",
        )
        book = BookKey("Source", "ספר")
        artifact = repo / book_key_to_relpath(book)
        write_artifact(artifact, [
            LinkRecord(
                book, 0, 0, 1, "Genesis 1:1",
                source_hash=content_hash("תוכן"),
            )
        ])
        return repo, snapshot, artifact

    def test_seed_validates_and_builds_release_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo, snapshot, _ = self._fixture(Path(temporary))
            output = repo / "stack" / "engine_fingerprint.txt"
            files, records = seed(repo, snapshot, output)
            self.assertEqual((files, records), (1, 1))
            manifest = json.loads(
                (repo / "line-baseline" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["engine_fingerprint"], self.FINGERPRINT)
            self.assertEqual(output.read_text(encoding="utf-8"), "alpha=1\nomega=2\n")

    def test_seed_rejects_artifact_source_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo, snapshot, artifact = self._fixture(Path(temporary))
            book = BookKey("Source", "ספר")
            write_artifact(artifact, [
                LinkRecord(
                    book, 0, 0, 1, "Genesis 1:1",
                    source_hash="0" * 16,
                )
            ])
            with self.assertRaisesRegex(RuntimeError, "source hash differs"):
                seed(repo, snapshot, repo / "stack" / "engine_fingerprint.txt")


if __name__ == "__main__":
    unittest.main()
