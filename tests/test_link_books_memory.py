import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import link_books  # noqa: E402
from linker_artifact import LinkRecord, content_hash, read_artifact, write_artifact  # noqa: E402


class WorkerMemoryTest(unittest.TestCase):
    def test_linux_rss_uses_current_proc_value_not_inherited_high_water_mark(self):
        # Linux keeps ru_maxrss across execve.  A recycled worker must ignore that
        # historical maximum and use the current resident pages from /proc instead.
        high_water = mock.Mock(ru_maxrss=9_999_999)
        with mock.patch.object(link_books.sys, "platform", "linux"), \
                mock.patch("builtins.open", mock.mock_open(read_data="100 7 0 0 0 0 0\n")), \
                mock.patch.object(link_books.os, "sysconf", return_value=4096), \
                mock.patch.object(link_books.resource, "getrusage", return_value=high_water):
            self.assertEqual(link_books.rss_bytes(), 7 * 4096)

    def test_zero_progress_over_cap_fails_instead_of_exec_loop(self):
        with self.assertRaisesRegex(RuntimeError, "zero-progress recycle loop"):
            link_books.recycle_needed(current_rss=101, cap=100, processed=0)

    def test_recycle_only_after_progress(self):
        self.assertFalse(link_books.recycle_needed(current_rss=100, cap=100, processed=0))
        self.assertTrue(link_books.recycle_needed(current_rss=101, cap=100, processed=1))

    def test_checkpointed_book_resumes_after_mid_book_recycle(self):
        class Doc:
            resolved_refs = []

        class Linker:
            def __init__(self):
                self.calls = []

            def bulk_link(self, texts, type_filter):
                self.calls.append(list(texts))
                return [Doc() for _ in texts]

        class RecycleSignal(Exception):
            pass

        bk = link_books.BookKey("source", "giant")
        lines = [(i, f"שורה {i}", f"Book {i}") for i in range(5)]
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = os.path.join(tmp, "checkpoint")
            output = os.path.join(tmp, "artifacts", "giant.jsonl")
            first = Linker()
            with mock.patch.object(link_books, "BATCH_LINES", 2), \
                    mock.patch.object(link_books, "RSS_CAP", 100), \
                    mock.patch.object(link_books, "rss_bytes", return_value=101):
                with self.assertRaises(RecycleSignal):
                    link_books.process_book_checkpointed(
                        first, bk, lines, lambda _line: None, lambda: None,
                        checkpoint, output,
                        lambda _rss, _batches: (_ for _ in ()).throw(RecycleSignal()),
                    )
            self.assertEqual(first.calls, [["שורה 0", "שורה 1"]])
            self.assertTrue(os.path.exists(os.path.join(checkpoint, "000000000000.jsonl")))
            self.assertFalse(os.path.exists(output), "partial book became publicly visible")

            second = Linker()
            with mock.patch.object(link_books, "BATCH_LINES", 2), \
                    mock.patch.object(link_books, "RSS_CAP", 100), \
                    mock.patch.object(link_books, "rss_bytes", return_value=0):
                count, words = link_books.process_book_checkpointed(
                    second, bk, lines, lambda _line: None, lambda: None,
                    checkpoint, output,
                    lambda _rss, _batches: self.fail("unexpected second recycle"),
                )
            self.assertEqual(second.calls, [["שורה 2", "שורה 3"], ["שורה 4"]])
            self.assertEqual(count, 0)
            self.assertEqual(words, 10)
            self.assertTrue(os.path.exists(output))

    def test_heading_lines_are_not_sent_and_context_is_forwarded(self):
        class Doc:
            resolved_refs = []

        class Linker:
            def __init__(self):
                self.calls = []

            def bulk_link(self, texts, book_context_refs, type_filter):
                self.calls.append((list(texts), list(book_context_refs), type_filter))
                return [Doc() for _ in texts]

        linker = Linker()
        records, words = link_books.process_book(
            linker,
            link_books.BookKey("source", "ספר"),
            [
                (0, "<h1>ספר</h1>", "ספר"),
                (1, '<img src="data:image/png;base64,AAAA">', "ספר"),
                (2, "ראו לקמן משנה א", "ברכות א, א"),
            ],
            lambda _line: None,
            lambda: None,
            context_ref_factory=lambda value: f"REF:{value}",
        )
        self.assertEqual(records, [])
        self.assertEqual(words, 4)
        self.assertEqual(
            linker.calls,
            [(["ראו לקמן משנה א"], ["REF:ברכות א, א"], "citation")],
        )

    def test_accumulation_unions_valid_prior_records_and_drops_stale_or_heading(self):
        bk = link_books.BookKey("source", "ספר")
        lines = [
            (0, "<h2>כותרת</h2>", "ספר"),
            (1, "ראו בראשית א", "ספר א"),
        ]
        heading_hash = content_hash(lines[0][1])
        prose_hash = content_hash(lines[1][1])
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = os.path.join(tmp, "checkpoint")
            output = os.path.join(tmp, "artifacts", "book.jsonl")
            write_artifact(output, [
                LinkRecord(bk, 0, 4, 10, "Genesis 1:1", source_hash=heading_hash),
                LinkRecord(bk, 1, 5, 12, "Genesis 1:1", source_hash=prose_hash),
                LinkRecord(bk, 1, 0, 3, "Exodus 1:1", source_hash="0" * 16),
            ])
            new = LinkRecord(bk, 1, 5, 12, "Genesis 1:2", source_hash=prose_hash)
            with mock.patch.object(
                link_books,
                "process_batch",
                return_value=([new], 3),
            ), mock.patch.object(link_books, "rss_bytes", return_value=0):
                count, _words = link_books.process_book_checkpointed(
                    object(), bk, lines, lambda _line: None, lambda: None,
                    checkpoint, output,
                    lambda _rss, _batches: self.fail("unexpected recycle"),
                    accumulate_existing=True,
                )
            records = list(read_artifact(output))
            self.assertEqual(count, 2)
            self.assertEqual(
                [(record.line_index, record.target_ref) for record in records],
                [(1, "Genesis 1:1"), (1, "Genesis 1:2")],
            )


if __name__ == "__main__":
    unittest.main()
