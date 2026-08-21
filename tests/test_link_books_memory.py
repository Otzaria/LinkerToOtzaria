import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import link_books  # noqa: E402


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

    def test_stale_heartbeat_cannot_steal_a_live_book_lock(self):
        """A slow resolver must never race a peer over its checkpoint directory."""
        with tempfile.TemporaryDirectory() as run:
            os.makedirs(os.path.join(run, "done"))
            first = link_books.BookClaim.acquire(run, "book", stale_seconds=1)
            self.assertIsNotNone(first)
            heartbeat = os.path.join(run, "claim", "book", "hb")
            old = time.time() - 2
            os.utime(heartbeat, (old, old))

            # Heartbeat age alone used to permit this second owner.  The kernel
            # lock proves the first worker is still alive, so no takeover occurs.
            self.assertIsNone(link_books.BookClaim.acquire(run, "book", stale_seconds=1))
            self.assertTrue(os.path.isdir(os.path.join(run, "claim", "book")))

            first.release()
            recovered = link_books.BookClaim.acquire(run, "book", stale_seconds=1)
            self.assertIsNotNone(recovered)
            recovered.release()

    def test_claim_heartbeat_never_recreates_lost_ownership(self):
        with tempfile.TemporaryDirectory() as run:
            os.makedirs(os.path.join(run, "done"))
            claim = link_books.BookClaim.acquire(run, "book")
            self.assertIsNotNone(claim)
            os.remove(os.path.join(run, "claim", "book", "hb"))
            with self.assertRaisesRegex(RuntimeError, "claim disappeared"):
                claim.heartbeat()
            claim.release()

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
        lines = [(i, f"line {i}") for i in range(5)]
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
            self.assertEqual(first.calls, [["line 0", "line 1"]])
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
            self.assertEqual(second.calls, [["line 2", "line 3"], ["line 4"]])
            self.assertEqual(count, 0)
            self.assertEqual(words, 10)
            self.assertTrue(os.path.exists(output))


if __name__ == "__main__":
    unittest.main()
