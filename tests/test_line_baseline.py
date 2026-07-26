import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from line_baseline import (  # noqa: E402
    build_line_baseline,
    compute_line_delta,
    indices_from_ranges,
    line_fingerprint,
    plan_changed_books,
    validate_baseline_identity,
)
from linker_artifact import (  # noqa: E402
    BookKey,
    LinkRecord,
    book_key_to_relpath,
    content_hash,
    read_artifact,
    write_artifact,
)
import link_books  # noqa: E402


def snapshot(path, rows):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE lines_snapshot("
        "source_name TEXT, canonical_he_title TEXT, line_index INTEGER, content TEXT)"
    )
    connection.executemany("INSERT INTO lines_snapshot VALUES(?,?,?,?)", rows)
    connection.commit()
    connection.close()


class LineBaselineTest(unittest.TestCase):
    BOOK = BookKey("Source", "ספר")

    def test_delta_reuses_stable_and_moved_identical_lines(self):
        old = [
            (0, line_fingerprint("א")),
            (1, line_fingerprint("ב")),
            (2, line_fingerprint("ג")),
        ]
        current = [(0, "א"), (1, "חדש"), (2, "ג"), (3, "ב")]
        delta = compute_line_delta(old, current)
        self.assertEqual(delta.reuse, ((0, 0), (2, 2), (1, 3)))
        self.assertEqual(indices_from_ranges(delta.ner_ranges), {1})
        self.assertEqual(delta.reused_line_count + delta.ner_line_count, len(current))

    def test_delta_does_not_reuse_identical_relative_text_at_new_context(self):
        old = [(4, line_fingerprint("ראה לקמן", "ברכות א, א"))]
        current = [(4, "ראה לקמן", "ברכות ב, א")]
        delta = compute_line_delta(old, current)
        self.assertEqual(delta.reuse, ())
        self.assertEqual(indices_from_ranges(delta.ner_ranges), {4})

    def test_release_baseline_identity_and_per_book_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "artifacts").mkdir()
            old_snapshot = repo / "old.db"
            snapshot(old_snapshot, [
                ("Source", "ספר", 0, "א"),
                ("Source", "ספר", 1, "ב"),
                ("Source", "ספר", 2, "ג"),
            ])
            artifact = repo / book_key_to_relpath(self.BOOK)
            write_artifact(artifact, [
                LinkRecord(
                    self.BOOK, 0, 0, 1, "Genesis 1:1",
                    source_hash=content_hash("א"),
                )
            ])
            baseline_hashes = {("Source", "ספר"): "1" * 16}
            build_line_baseline(
                str(old_snapshot),
                str(repo / "line-baseline"),
                current_hashes=baseline_hashes,
                snapshot_sha256="2" * 64,
                engine_fingerprint="engine-v1",
                artifacts_root=str(repo / "artifacts"),
            )
            self.assertTrue(validate_baseline_identity(
                str(repo / "line-baseline"),
                snapshot_sha256="2" * 64,
                engine_fingerprint="engine-v1",
                book_count=1,
            ))
            new_snapshot = repo / "new.db"
            snapshot(new_snapshot, [
                ("Source", "ספר", 0, "א"),
                ("Source", "ספר", 1, "חדש"),
                ("Source", "ספר", 2, "ג"),
            ])
            plans, reused, ner = plan_changed_books(
                str(new_snapshot),
                [self.BOOK],
                baseline_root=str(repo / "line-baseline"),
                baseline_hashes=baseline_hashes,
                baseline_identity_valid=True,
            )
            delta = plans[("Source", "ספר")]
            self.assertEqual((reused, ner), (2, 1))
            self.assertIsNotNone(delta.prior_artifact_sha256)

            # A bad committed book hash disables only this optimisation; correctness
            # falls back to NER for every current line.
            plans, reused, ner = plan_changed_books(
                str(new_snapshot),
                [self.BOOK],
                baseline_root=str(repo / "line-baseline"),
                baseline_hashes={("Source", "ספר"): "f" * 16},
                baseline_identity_valid=True,
            )
            self.assertEqual((reused, ner), (0, 3))

    def test_checkpointed_merge_rewrites_only_unmatched_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / book_key_to_relpath(self.BOOK)
            old_records = [
                LinkRecord(
                    self.BOOK, 0, 0, 1, "Genesis 1:1",
                    source_hash=content_hash("א"),
                ),
                LinkRecord(
                    self.BOOK, 2, 0, 1, "Exodus 1:1",
                    source_hash=content_hash("ג"),
                ),
            ]
            write_artifact(artifact, old_records)
            original = link_books.process_batch
            original_batch_lines = link_books.BATCH_LINES

            def fake_process(_linker, book, batch, _log, **_kwargs):
                self.assertEqual(batch, [(1, "חדש", "ספר")])
                return [
                    LinkRecord(
                        book, 1, 0, 2, "Numbers 1:1",
                        source_hash=content_hash("חדש"),
                    )
                ], 1

            link_books.process_batch = fake_process
            link_books.BATCH_LINES = 1
            try:
                count, _ = link_books.process_book_checkpointed(
                    object(),
                    self.BOOK,
                    [(0, "א", "ספר"), (1, "חדש", "ספר"), (2, "ג", "ספר")],
                    lambda _message: None,
                    lambda: None,
                    str(root / "checkpoints"),
                    str(artifact),
                    lambda *_args: None,
                    ner_indices={1},
                    reuse=((0, 0), (2, 2)),
                )
            finally:
                link_books.process_batch = original
                link_books.BATCH_LINES = original_batch_lines
            self.assertEqual(count, 3)
            self.assertEqual(
                sorted(path.name for path in (root / "checkpoints").iterdir()),
                ["000000000001.jsonl"],
            )
            records = list(read_artifact(artifact))
            self.assertEqual(
                [(record.line_index, record.target_ref) for record in records],
                [
                    (0, "Genesis 1:1"),
                    (1, "Numbers 1:1"),
                    (2, "Exodus 1:1"),
                ],
            )


if __name__ == "__main__":
    unittest.main()
