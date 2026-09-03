"""Heavy-book deferral: a book that balloons a resolver is deferred, not dropped.

A few texts grow a worker by many GB inside one book (12.5 GB on 2026-09-03) and
took the WSL VM down four times. The engine now (1) defers such a book between
batches, keeping its finished shards, (2) resolves deferred books only after every
ordinary book is done, at most HEAVY_BOOK_SLOTS at a time, and (3) can bound a
worker's address space so a runaway line ends in a MemoryError inside that worker.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import link_books  # noqa: E402


class _Doc:
    resolved_refs = []


class _Linker:
    def __init__(self):
        self.calls = []

    def bulk_link(self, texts, book_context_refs=None, type_filter=None):
        self.calls.append(list(texts))
        return [_Doc() for _ in texts]


class DeferralTest(unittest.TestCase):
    def test_book_that_grows_past_the_threshold_is_deferred_with_its_shards_kept(self):
        bk = link_books.BookKey("source", "giant")
        lines = [(i, f"שורה {i}", "ספר") for i in range(5)]
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = os.path.join(tmp, "checkpoint")
            output = os.path.join(tmp, "artifacts", "giant.jsonl")
            first = _Linker()
            # 1 GB at book start, 5 GB after the first batch: +4 GB > 2.5 GB threshold.
            with mock.patch.object(link_books, "BATCH_LINES", 2), \
                    mock.patch.object(link_books, "RSS_CAP", 1e12), \
                    mock.patch.object(link_books, "HEAVY_BOOK_GROWTH_BYTES", 2.5e9), \
                    mock.patch.object(link_books, "worker_memory_bytes", side_effect=[1e9, 5e9, 5e9]):
                with self.assertRaises(link_books.BookDeferred) as ctx:
                    link_books.process_book_checkpointed(
                        first, bk, lines, lambda _l: None, lambda: None, checkpoint, output,
                        lambda _rss, _b: self.fail("deferral must win over recycling"),
                    )
            self.assertEqual(ctx.exception.book_key, bk)
            self.assertEqual(ctx.exception.growth_bytes, 4e9)
            self.assertEqual(first.calls, [["שורה 0", "שורה 1"]])
            self.assertTrue(os.path.exists(os.path.join(checkpoint, "000000000000.jsonl")))
            self.assertFalse(os.path.exists(output), "partial book became publicly visible")

            # Heavy phase: same book, deferral disabled, resumes from the kept shard.
            second = _Linker()
            with mock.patch.object(link_books, "BATCH_LINES", 2), \
                    mock.patch.object(link_books, "RSS_CAP", 1e12), \
                    mock.patch.object(link_books, "worker_memory_bytes", return_value=9e9):
                count, words = link_books.process_book_checkpointed(
                    second, bk, lines, lambda _l: None, lambda: None, checkpoint, output,
                    lambda _rss, _b: self.fail("unexpected recycle"), defer_when_heavy=False,
                )
            self.assertEqual(second.calls, [["שורה 2", "שורה 3"], ["שורה 4"]])
            self.assertEqual((count, words), (0, 10))
            self.assertTrue(os.path.exists(output))

    def test_heavy_phase_uses_its_own_recycle_cap(self):
        # Under a slot the ordinary cap (1 byte here) must not recycle the worker;
        # the heavy cap (huge) governs, so the book completes in one life.
        bk = link_books.BookKey("source", "deferred")
        lines = [(i, f"שורה {i}", "ספר") for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(link_books, "BATCH_LINES", 2), \
                    mock.patch.object(link_books, "RSS_CAP", 1), \
                    mock.patch.object(link_books, "worker_memory_bytes", return_value=5e9):
                count, _ = link_books.process_book_checkpointed(
                    _Linker(), bk, lines, lambda _l: None, lambda: None,
                    os.path.join(tmp, "cp"), os.path.join(tmp, "out.jsonl"),
                    lambda _rss, _b: self.fail("ordinary cap must not apply under a slot"),
                    defer_when_heavy=False, recycle_cap=1e12,
                )
            self.assertEqual(count, 0)

    def test_growth_below_the_threshold_does_not_defer(self):
        bk = link_books.BookKey("source", "normal")
        lines = [(i, f"שורה {i}", "ספר") for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(link_books, "BATCH_LINES", 2), \
                    mock.patch.object(link_books, "RSS_CAP", 1e12), \
                    mock.patch.object(link_books, "HEAVY_BOOK_GROWTH_BYTES", 2.5e9), \
                    mock.patch.object(link_books, "worker_memory_bytes", return_value=1e9):
                count, _ = link_books.process_book_checkpointed(
                    _Linker(), bk, lines, lambda _l: None, lambda: None,
                    os.path.join(tmp, "cp"), os.path.join(tmp, "out.jsonl"),
                    lambda _rss, _b: self.fail("unexpected recycle"),
                )
            self.assertEqual(count, 0)


class OrderingAndMarkersTest(unittest.TestCase):
    def test_partition_puts_deferred_books_after_ordinary_ones_and_skips_done(self):
        books = [link_books.BookKey("s", f"b{i}") for i in range(4)]
        with tempfile.TemporaryDirectory() as run:
            os.makedirs(os.path.join(run, "done"))
            open(os.path.join(run, "done", link_books.claim_id(books[0])), "w").close()
            link_books.mark_heavy(run, link_books.claim_id(books[2]), books[2], 3_000_000_000, "test")
            normal, heavy = link_books.partition_pending(books, run)
            self.assertEqual(normal, [books[1], books[3]])
            self.assertEqual(heavy, [books[2]])
            marker = link_books.heavy_marker_path(run, link_books.claim_id(books[2]))
            self.assertTrue(os.path.exists(marker))
            import json
            with open(marker, encoding="utf-8") as fh:
                payload = json.load(fh)
            self.assertEqual(payload["canonical_he_title"], "b2")
            self.assertEqual(payload["growth_bytes"], 3_000_000_000)

    def test_marked_book_waits_for_the_heavy_phase_even_when_seen_by_a_peer(self):
        # The OOM scenario: worker A defers X, worker B (still walking its ordinary
        # list) meets X's marker a moment later. B must skip it - not take a slot and
        # run it at the heavy cap beside ten ordinary workers.
        bk = link_books.BookKey("s", "x")
        cid = link_books.claim_id(bk)
        with tempfile.TemporaryDirectory() as run:
            self.assertEqual(link_books.gate_heavy_book(run, cid, heavy_phase=False), "normal")
            link_books.mark_heavy(run, cid, bk, 900_000_000, "test")
            self.assertEqual(link_books.gate_heavy_book(run, cid, heavy_phase=False), "skip")
            self.assertEqual(link_books.gate_heavy_book(run, cid, heavy_phase=True), "heavy")

    def test_memory_error_is_recognised_through_process_batch_wrapping(self):
        try:
            try:
                raise MemoryError("boom")
            except MemoryError as inner:
                raise RuntimeError("line 7 failed to link: MemoryError: boom") from inner
        except RuntimeError as wrapped:
            self.assertTrue(link_books.is_memory_error(wrapped))
        self.assertTrue(link_books.is_memory_error(MemoryError()))
        self.assertFalse(link_books.is_memory_error(RuntimeError("other")))


@unittest.skipUnless(sys.platform.startswith("linux"), "flock/RLIMIT_AS are POSIX")
class HeavySlotAndLimitTest(unittest.TestCase):
    def test_slots_bound_concurrency_and_are_reusable(self):
        with tempfile.TemporaryDirectory() as run:
            first = link_books.HeavySlot.acquire(run, 2)
            second = link_books.HeavySlot.acquire(run, 2)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertNotEqual(first.path, second.path)
            # flock is per open file description: a second acquire in this process
            # would succeed on the same fd family, so prove exclusion from a child.
            pid = os.fork()
            if pid == 0:
                third = link_books.HeavySlot.acquire(run, 2)
                os._exit(0 if third is None else 1)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.WEXITSTATUS(status), 0, "third acquire must fail while two are held")
            first.release()
            pid = os.fork()
            if pid == 0:
                freed = link_books.HeavySlot.acquire(run, 2)
                os._exit(0 if freed is not None else 1)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.WEXITSTATUS(status), 0, "a released slot is available again")
            second.release()

    def test_address_space_limit_applies_within_the_hard_limit(self):
        import resource
        pid = os.fork()
        if pid == 0:  # never lower the test process's own limit
            applied = link_books.apply_address_space_limit(40 * 1024 ** 3)
            soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
            os._exit(0 if applied == soft == 40 * 1024 ** 3 else 1)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.WEXITSTATUS(status), 0)
        self.assertEqual(link_books.apply_address_space_limit(0), 0)


if __name__ == "__main__":
    unittest.main()
