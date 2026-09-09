"""Fresh workers for heavy books, and one requeue after a MemoryError.

Audit of cycle 33987355439 (item L5, reports 10/11-relink). Recovery relink
34016397157 linked 5,126 of 5,127 books in 1h44m and then failed the whole run on
the last one:

    05:07:52 w06 ERROR 'משנה למלך על משנה תורה, הלכות מלווה ולווה':
             RuntimeError: line 43 failed to link: MemoryError:

The book was not poison. The next run linked it in **2.3 s** on a worker whose
first book it was, and three earlier deferrals in the same run each show one line
demanding 11.86-12.22 GB. What differed was the worker: w06 had `processed 75 this
life`, so its accumulated Ref cache plus one line's 11.9 GB crossed the 26 GB
RLIMIT_AS ceiling. Guest memory was not the constraint (MemAvailable 27.4 GB at the
fatal moment, MemTotal 42.1 GB).

Two rules follow, and these tests pin them:

  * a deferred (heavy) book is taken only by a worker with a fresh life; a worker
    that is not fresh recycles BEFORE claiming it, so nothing is held and the book
    is left unclaimed and undone for the fresh image;
  * a MemoryError in the heavy phase requeues the book exactly once (O_EXCL marker)
    and fails the run on the second, naming both attempts' growth and the ceilings.

Windows: `link_books` imports POSIX-only `resource`/`fcntl`, so the engine-side
cases are skipped there (as the repo's other engine tests are). The driver-side
summary line is exercised on every platform in tests/test_incremental.py.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

import incremental  # noqa: E402

try:  # pragma: no cover - platform gate
    import link_books
except ImportError:  # Windows: link_books imports POSIX-only `resource`
    link_books = None

ENGINE = unittest.skipIf(
    link_books is None,
    "link_books requires POSIX `resource`/`fcntl`; the engine rules are Linux-only",
)

# One real process that takes the book the way the loop does, so "the claim was not
# leaked" is proved against a foreign process's kernel lock, not this one's.
CLAIMER = """
import os, sys
sys.path.insert(0, {src!r})
import link_books

run, cid, result = sys.argv[1:4]
claim = link_books.BookClaim.acquire(run, cid)
with open(result, "w", encoding="utf-8") as fh:
    fh.write("won" if claim is not None else "lost")
if claim is not None:
    claim.release()
"""


def _book(title="הלכות מלווה ולווה"):
    return link_books.BookKey("Sefaria", title)


class _Life:
    """One worker life driving the exact branch order of `_worker_loop`.

    gate_heavy_book -> fresh-worker check -> HeavySlot -> BookClaim -> mark_done.
    `growth` is what worker_memory_bytes() would report above this life's baseline;
    a recycle ends the life (the pool forks a fresh child, `processed` back to 0).
    """

    def __init__(self, run, label, growth=0, heavy_phase=True):
        self.run = run
        self.label = label
        self.growth = growth
        self.heavy_phase = heavy_phase
        self.processed = 0
        self.recycled_for = []
        self.completed = []
        self.claimed = []
        self.requeued = []
        self.failed = []

    def offer(self, bk, memory_error=None):
        """Offer one pending book to this life; True while the life continues.

        ``memory_error`` simulates process_book_checkpointed raising the wrapped
        per-line MemoryError, so the requeue branch is driven in the same order the
        loop drives it.
        """
        cid = link_books.claim_id(bk)
        gate = link_books.gate_heavy_book(self.run, cid, self.heavy_phase)
        if gate == "skip":
            return True
        heavy = gate == "heavy"
        if heavy and link_books.HEAVY_FRESH_WORKER and not \
                link_books.worker_is_fresh_for_heavy(self.processed, self.growth):
            link_books.journal_memory_event(self.run, {
                "event": "recycled-for-heavy", "claim_id": cid,
                "source_name": bk.source_name,
                "canonical_he_title": bk.canonical_he_title,
                "worker": self.label, "processed": self.processed,
                "growth_bytes": int(self.growth),
            })
            self.recycled_for.append(cid)
            return False  # the life ends here: nothing claimed, nothing held
        slot = link_books.HeavySlot.acquire(self.run, 2) if heavy else None
        claim = link_books.BookClaim.acquire(self.run, cid)
        try:
            if claim is None:
                return True
            self.claimed.append(cid)
            if memory_error is not None:
                return self._on_memory_error(bk, cid, memory_error, heavy)
            link_books.mark_done(self.run, cid)
            self.completed.append(cid)
            self.processed += 1
        finally:
            if claim is not None:
                claim.release()
            if slot is not None:
                slot.release()
        return True

    def _on_memory_error(self, bk, cid, growth, heavy):
        """The loop's `except Exception` branch for a MemoryError in the heavy phase."""
        attempt = {
            "claim_id": cid, "source_name": bk.source_name,
            "canonical_he_title": bk.canonical_he_title, "worker": self.label,
            "processed": self.processed, "growth_bytes": int(growth), "line": 43,
            "phase": "heavy" if heavy else "normal",
            "limits": link_books.current_memory_limits(heavy),
        }
        won, first = link_books.claim_memory_requeue(self.run, cid, attempt)
        if won:
            link_books.journal_memory_event(
                self.run, {"event": "requeued-after-memoryerror", **attempt})
            self.requeued.append(cid)
            return False  # the worker recycles; the book stays undone and unclaimed
        note = link_books.format_memory_failure(
            bk=bk, first=first, second=attempt, limits=attempt["limits"])
        os.makedirs(os.path.join(self.run, "failed"), exist_ok=True)
        with open(os.path.join(self.run, "failed", cid), "w", encoding="utf-8") as fh:
            json.dump({**bk.to_dict(), "note": note}, fh, ensure_ascii=False)
        link_books.mark_done(self.run, cid)  # poison-book loop guard, as today
        self.failed.append(cid)
        return True


@ENGINE
class FreshWorkerForHeavyBooksTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = os.path.join(self.temp.name, "run")
        os.makedirs(os.path.join(self.run, "done"))
        self.book = _book()
        self.cid = link_books.claim_id(self.book)
        link_books.mark_heavy(self.run, self.cid, self.book, 11_888_857_088,
                              "RuntimeError: line 43 failed to link: MemoryError:")

    def tearDown(self):
        self.temp.cleanup()

    def test_freshness_rule_is_a_disjunction_that_cannot_loop(self):
        # A fresh image is fresh whatever the memory reading says: that is what stops
        # the rule from recycling the same image forever.
        self.assertTrue(link_books.worker_is_fresh_for_heavy(0, 10 ** 12))
        self.assertTrue(link_books.worker_is_fresh_for_heavy(0, 0))
        # A life that completed books but grew almost nothing keeps the book (no fork).
        self.assertTrue(link_books.worker_is_fresh_for_heavy(
            3, link_books.HEAVY_FRESH_GROWTH_BYTES))
        # w06's shape in relink 34016397157: 75 books, a full Ref cache.
        self.assertFalse(link_books.worker_is_fresh_for_heavy(
            75, link_books.HEAVY_FRESH_GROWTH_BYTES + 1))

    def test_stale_worker_recycles_before_claiming_and_leaks_nothing(self):
        stale = _Life(self.run, "w06", growth=1_100_000_000)
        stale.processed = 75
        self.assertFalse(stale.offer(self.book), "a stale life must end at a heavy book")
        self.assertEqual(stale.recycled_for, [self.cid])
        self.assertEqual(stale.claimed, [], "the book must not be claimed by a stale life")

        # Nothing leaked: no claim directory, no lock held, the rescan still hands the
        # book out, and a FOREIGN process can take it.
        self.assertFalse(os.path.exists(os.path.join(self.run, "claim", self.cid)))
        self.assertFalse(link_books.BookClaim.is_claimed(self.run, self.cid))
        self.assertEqual(link_books.unclaimed([self.book], self.run), [self.book])
        self.assertFalse(os.path.exists(os.path.join(self.run, "done", self.cid)))
        # ... and no heavy slot was taken while deciding to recycle.
        first = link_books.HeavySlot.acquire(self.run, 1)
        self.assertIsNotNone(first, "a heavy slot was held across the recycle")
        first.release()

        with tempfile.TemporaryDirectory() as work:
            result = os.path.join(work, "result")
            script = os.path.join(work, "claimer.py")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(CLAIMER.format(src=os.path.join(ROOT, "src")))
            subprocess.run([sys.executable, script, self.run, self.cid, result],
                           check=True, timeout=60)
            with open(result, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "won")

    def test_the_fresh_image_after_the_recycle_completes_the_book(self):
        stale = _Life(self.run, "w06", growth=1_100_000_000)
        stale.processed = 75
        self.assertFalse(stale.offer(self.book))
        fresh = _Life(self.run, "w06")  # the pool forks a fresh child with the same label
        self.assertTrue(fresh.offer(self.book))
        self.assertEqual(fresh.completed, [self.cid])
        self.assertTrue(os.path.isfile(os.path.join(self.run, "done", self.cid)))
        self.assertEqual(link_books.count_memory_events(self.run, "recycled-for-heavy"), 1)

    def test_every_stale_peer_recycles_once_and_the_book_is_never_stranded(self):
        # Twelve workers reach the heavy phase full.  Each must recycle exactly once
        # for this book, and the first fresh image must finish it: `unclaimed()` may
        # never hide a heavy book from every worker.
        lives = [_Life(self.run, f"w{n:02d}", growth=900_000_000) for n in range(1, 13)]
        for life in lives:
            life.processed = 40 + lives.index(life)
            self.assertFalse(life.offer(self.book))
        self.assertEqual(link_books.count_memory_events(self.run, "recycled-for-heavy"), 12)
        self.assertEqual(link_books.unclaimed([self.book], self.run), [self.book])
        fresh = [_Life(self.run, life.label) for life in lives]
        for life in fresh:
            self.assertTrue(life.offer(self.book))
        finishers = [life.label for life in fresh if life.completed]
        self.assertEqual(len(finishers), 1, "exactly one fresh worker links the book")
        self.assertEqual(link_books.count_claim_events(self.run, "duplicate"), 0)

    def test_ordinary_books_are_untouched_by_the_rule(self):
        plain = _book("ספר רגיל")
        life = _Life(self.run, "w03", growth=5_000_000_000)
        life.processed = 500
        self.assertTrue(life.offer(plain), "a normal book must never force a recycle")
        self.assertEqual(life.completed, [link_books.claim_id(plain)])
        self.assertEqual(link_books.count_memory_events(self.run, "recycled-for-heavy"), 0)

    def test_the_rule_can_be_switched_off(self):
        from unittest import mock
        life = _Life(self.run, "w06", growth=9_000_000_000)
        life.processed = 75
        with mock.patch.object(link_books, "HEAVY_FRESH_WORKER", False):
            self.assertTrue(life.offer(self.book))
        self.assertEqual(life.completed, [self.cid])


@ENGINE
class RequeueAfterMemoryErrorTest(unittest.TestCase):
    """The O_EXCL budget: one retry per book, then the run fails."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = os.path.join(self.temp.name, "run")
        os.makedirs(self.run)
        self.book = _book()
        self.cid = link_books.claim_id(self.book)

    def tearDown(self):
        self.temp.cleanup()

    def _attempt(self, worker, processed, growth, line=43):
        return {
            "claim_id": self.cid,
            "source_name": self.book.source_name,
            "canonical_he_title": self.book.canonical_he_title,
            "worker": worker, "processed": processed,
            "growth_bytes": growth, "line": line, "phase": "heavy",
            "error": f"RuntimeError: line {line} failed to link: MemoryError:",
            "limits": {"address_space_bytes": 26_000_000_000,
                       "recycle_cap_bytes": 5_000_000_000,
                       "heavy_growth_bytes": 800_000_000},
        }

    def test_first_memory_error_wins_the_budget_and_the_second_does_not(self):
        first_attempt = self._attempt("w06", 75, 11_888_857_088)
        won, stored = link_books.claim_memory_requeue(self.run, self.cid, first_attempt)
        self.assertTrue(won)
        self.assertEqual(stored["growth_bytes"], 11_888_857_088)
        self.assertTrue(os.path.isfile(link_books.requeue_marker_path(self.run, self.cid)))

        second_attempt = self._attempt("w01", 0, 11_946_172_416, line=4)
        won_again, first = link_books.claim_memory_requeue(self.run, self.cid, second_attempt)
        self.assertFalse(won_again, "a book may be requeued once, never twice")
        self.assertEqual(first["worker"], "w06")
        self.assertEqual(first["processed"], 75)
        # A different book still has its own budget.
        other = link_books.claim_id(_book("ספר אחר"))
        self.assertTrue(link_books.claim_memory_requeue(self.run, other, {})[0])

    def test_only_one_of_twelve_simultaneous_workers_may_requeue(self):
        results = []
        for label in (f"w{n:02d}" for n in range(1, 13)):
            results.append(link_books.claim_memory_requeue(
                self.run, self.cid, self._attempt(label, 5, 12_000_000_000))[0])
        self.assertEqual(sum(1 for won in results if won), 1)

    def test_a_torn_marker_fails_the_book_rather_than_granting_a_second_retry(self):
        path = link_books.requeue_marker_path(self.run, self.cid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()  # created, never written (worker killed between the two)
        won, first = link_books.claim_memory_requeue(self.run, self.cid, self._attempt("w02", 1, 1))
        self.assertFalse(won)
        self.assertEqual(first, {})

    def test_requeue_line_names_the_book_the_growth_the_worker_and_its_book_count(self):
        line = link_books.format_memory_requeue_line(
            bk=self.book, growth_bytes=11_888_857_088, worker="w06",
            processed=75, line_number=43,
        )
        self.assertEqual(
            line,
            f"memory: requeued Sefaria/{self.book.canonical_he_title!r} once after "
            "MemoryError (grew 11.9 GB in one line (line 43) on w06 after 75 books; "
            "retrying on a fresh worker)",
        )
        # A line-less MemoryError still reports the growth and the worker's life.
        self.assertIn(
            "grew 11.9 GB in one line on w01 after 1 book;",
            link_books.format_memory_requeue_line(
                bk=self.book, growth_bytes=11_888_857_088, worker="w01", processed=1),
        )

    def test_failure_message_names_the_book_both_attempts_and_the_live_limits(self):
        message = link_books.format_memory_failure(
            bk=self.book,
            first=self._attempt("w06", 75, 11_888_857_088),
            second=self._attempt("w01", 0, 12_217_487_360, line=4),
            limits={"address_space_bytes": 26_000_000_000,
                    "recycle_cap_bytes": 5_000_000_000,
                    "heavy_growth_bytes": 800_000_000},
        )
        self.assertIn(self.book.canonical_he_title, message)
        self.assertIn("MemoryError twice - the book had already spent its single requeue",
                      message)
        self.assertIn("attempt 1 grew 11.9 GB in one line (line 43) on w06 after 75 books",
                      message)
        self.assertIn("attempt 2 grew 12.2 GB in one line (line 4) on w01 after 0 books",
                      message)
        self.assertIn("address space 26000000000", message)
        self.assertIn("recycle cap 5000000000", message)
        self.assertIn("heavy deferral threshold 800000000", message)
        self.assertEqual(message.count("\n"), 0, "the failure message must stay one line")

    def test_an_incomplete_first_record_is_reported_instead_of_being_invented(self):
        message = link_books.format_memory_failure(
            bk=self.book, first={}, second=self._attempt("w01", 0, 1_000_000_000),
            limits={},
        )
        self.assertIn("figures unavailable", message)
        self.assertIn("address space unlimited", message)

    def test_line_number_is_read_from_the_wrapped_message_only_when_present(self):
        self.assertEqual(
            link_books.memory_error_line_number("line 43 failed to link: MemoryError:"), 43)
        self.assertIsNone(link_books.memory_error_line_number("MemoryError"))
        self.assertIsNone(link_books.memory_error_line_number(""))


@ENGINE
class HeavyPhaseScenarioTest(unittest.TestCase):
    """The two paths composed the way the resolve loop composes them.

    Relink 34016397157's ending, replayed: a deferred book, a MemoryError under a
    heavy slot, and then either the fresh retry that the run never got, or a second
    failure that must stop the run rather than loop on the book.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = os.path.join(self.temp.name, "run")
        os.makedirs(os.path.join(self.run, "done"))
        self.book = _book()
        self.cid = link_books.claim_id(self.book)
        link_books.mark_heavy(self.run, self.cid, self.book, 11_856_330_752,
                              "RuntimeError: line 43 failed to link: MemoryError:")

    def tearDown(self):
        self.temp.cleanup()

    def test_one_memory_error_is_requeued_and_the_retry_completes_the_book(self):
        first = _Life(self.run, "w06")
        self.assertFalse(first.offer(self.book, memory_error=11_888_857_088),
                         "the requeueing worker must end its life")
        self.assertEqual(first.requeued, [self.cid])
        self.assertEqual(first.failed, [])
        # Left undone, unclaimed and still marked heavy: the fresh image finds it.
        self.assertFalse(os.path.exists(os.path.join(self.run, "done", self.cid)))
        self.assertEqual(link_books.unclaimed([self.book], self.run), [self.book])
        _normal, heavy = link_books.partition_pending([self.book], self.run)
        self.assertEqual(heavy, [self.book])

        retry = _Life(self.run, "w06")
        self.assertTrue(retry.offer(self.book))
        self.assertEqual(retry.completed, [self.cid])
        stats = incremental.read_memory_stats(self.run)
        self.assertEqual((stats["requeued"], stats["succeeded"], stats["failed"]), (1, 1, 0))
        self.assertEqual(incremental.read_failed_books(self.run), set())
        self.assertEqual(
            incremental.format_memory_summary(requeued=1, succeeded=1, failed=0),
            "memory: 1 book requeued after MemoryError, 1 succeeded on retry, 0 failed",
        )

    def test_a_second_memory_error_fails_the_run_and_names_the_book_and_both_attempts(self):
        first = _Life(self.run, "w06")
        first.processed = 0
        self.assertFalse(first.offer(self.book, memory_error=11_888_857_088))
        retry = _Life(self.run, "w01")
        self.assertTrue(retry.offer(self.book, memory_error=12_217_487_360),
                        "the second failure must not silently recycle for ever")
        self.assertEqual(retry.failed, [self.cid])
        self.assertEqual(retry.requeued, [])

        # Exactly today's failure semantics: a `failed` marker, so the driver refuses
        # to advance the baseline, plus the `done` marker that stops this run retrying.
        self.assertEqual(incremental.read_failed_books(self.run),
                         {(self.book.source_name, self.book.canonical_he_title)})
        self.assertTrue(os.path.exists(os.path.join(self.run, "done", self.cid)))
        note = incremental.read_failed_notes(self.run)[
            (self.book.source_name, self.book.canonical_he_title)]
        self.assertIn("grew 11.9 GB", note)
        self.assertIn("grew 12.2 GB", note)
        self.assertIn("address space", note)
        stats = incremental.read_memory_stats(self.run)
        self.assertEqual((stats["requeued"], stats["succeeded"], stats["failed"]), (1, 0, 1))

        # A third worker meeting the same book finds it done: no loop.
        third = _Life(self.run, "w02")
        self.assertTrue(third.offer(self.book))
        self.assertEqual(third.completed, [])
        self.assertEqual(third.claimed, [])

    def test_the_requeue_and_the_fresh_worker_rule_compose(self):
        # The full 34016397157 ending: a full worker meets the deferred book, recycles
        # without claiming it, its fresh image MemoryErrors and requeues once, and the
        # next fresh image links it.  One recycle-for-heavy, one requeue, no duplicate.
        full = _Life(self.run, "w06", growth=1_100_000_000)
        full.processed = 75
        self.assertFalse(full.offer(self.book))
        self.assertEqual(full.claimed, [])
        fresh = _Life(self.run, "w06")
        self.assertFalse(fresh.offer(self.book, memory_error=11_888_857_088))
        final = _Life(self.run, "w06")
        self.assertTrue(final.offer(self.book))
        self.assertEqual(final.completed, [self.cid])
        stats = incremental.read_memory_stats(self.run)
        self.assertEqual(stats["recycled_for_heavy"], 1)
        self.assertEqual((stats["requeued"], stats["succeeded"], stats["failed"]), (1, 1, 0))
        self.assertEqual(link_books.count_claim_events(self.run, "duplicate"), 0)


@ENGINE
class MemoryJournalTest(unittest.TestCase):
    def test_events_survive_many_appenders_and_a_torn_final_line(self):
        with tempfile.TemporaryDirectory() as run:
            for index in range(20):
                link_books.journal_memory_event(run, {
                    "event": "recycled-for-heavy", "claim_id": f"{index:040d}"})
            link_books.journal_memory_event(run, {"event": "requeued-after-memoryerror",
                                                 "claim_id": "f" * 40})
            with open(link_books.memory_journal_path(run), "a", encoding="utf-8") as fh:
                fh.write('{"event": "recycled-for-hea')  # killed mid-write
            self.assertEqual(link_books.count_memory_events(run, "recycled-for-heavy"), 20)
            self.assertEqual(
                link_books.count_memory_events(run, "requeued-after-memoryerror"), 1)
            self.assertTrue(all("unix" in event
                                for event in link_books.read_memory_journal(run)))

    def test_a_missing_journal_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as run:
            self.assertEqual(link_books.read_memory_journal(run), [])
            self.assertEqual(link_books.count_memory_events(run, "recycled-for-heavy"), 0)


@ENGINE
class DriverSummaryTest(unittest.TestCase):
    """What the driver reports from the journal + the done/failed ledgers."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = os.path.join(self.temp.name, "run")
        for name in ("done", "failed"):
            os.makedirs(os.path.join(self.run, name))
        self.book = _book()
        self.cid = link_books.claim_id(self.book)

    def tearDown(self):
        self.temp.cleanup()

    def _requeued(self, cid):
        link_books.journal_memory_event(self.run, {
            "event": "requeued-after-memoryerror", "claim_id": cid,
            "source_name": "Sefaria", "canonical_he_title": "x",
            "growth_bytes": 11_888_857_088, "worker": "w06", "processed": 75})

    def test_a_requeued_book_that_completes_counts_as_succeeded(self):
        self._requeued(self.cid)
        self._requeued(self.cid)  # journalled twice, one BOOK
        open(os.path.join(self.run, "done", self.cid), "w").close()
        stats = incremental.read_memory_stats(self.run)
        self.assertEqual((stats["requeued"], stats["succeeded"], stats["failed"]), (1, 1, 0))
        self.assertEqual(
            incremental.format_memory_summary(**{k: stats[k] for k in
                                                 ("requeued", "succeeded", "failed")}),
            "memory: 1 book requeued after MemoryError, 1 succeeded on retry, 0 failed",
        )

    def test_a_requeued_book_that_fails_again_counts_as_failed_not_succeeded(self):
        self._requeued(self.cid)
        # The engine writes BOTH markers on the failure path (the done marker is the
        # poison-book loop guard); the summary must not read that as a success.
        open(os.path.join(self.run, "done", self.cid), "w").close()
        with open(os.path.join(self.run, "failed", self.cid), "w", encoding="utf-8") as fh:
            json.dump(self.book.to_dict(), fh, ensure_ascii=False)
        stats = incremental.read_memory_stats(self.run)
        self.assertEqual((stats["requeued"], stats["succeeded"], stats["failed"]), (1, 0, 1))

    def test_recycles_for_heavy_are_counted_per_event(self):
        for _ in range(12):
            link_books.journal_memory_event(self.run, {
                "event": "recycled-for-heavy", "claim_id": self.cid})
        self.assertEqual(incremental.read_memory_stats(self.run)["recycled_for_heavy"], 12)


@ENGINE
class CheckpointIsolationTest(unittest.TestCase):
    """The requeue journal is run-scoped state: it survives a save/restore by being
    excluded from it, and can never come back as a completed book."""

    def test_journal_and_marker_are_not_checkpoint_members_and_are_not_adopted(self):
        import argparse
        from pathlib import Path

        from ci import local_checkpoint_cache as cache

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, repo, cache_root = root / "run", root / "repo", root / "cache"
            book = {"source_name": "Sefaria", "canonical_he_title": "הלכות מלווה ולווה",
                    "hash": "1" * 16, "ner_ranges": [[0, 1]], "reuse": []}
            cid = link_books.claim_id(
                link_books.BookKey(book["source_name"], book["canonical_he_title"]))
            (run / "checkpoints" / cid).mkdir(parents=True)
            (run / "changed_books.json").write_text(json.dumps([book]), encoding="utf-8")
            (run / "checkpoints" / cid / "000000000000.jsonl").write_text(
                '{"record":"exact"}\n', encoding="utf-8")

            # The book was requeued and is NOT done: exactly the state a checkpoint
            # save from a live run can catch.
            link_books.journal_memory_event(str(run), {
                "event": "requeued-after-memoryerror", "claim_id": cid,
                "growth_bytes": 11_888_857_088, "worker": "w06", "processed": 75})
            link_books.claim_memory_requeue(str(run), cid, {"worker": "w06"})

            args = argparse.Namespace(
                cache_root=str(cache_root), run_dir=str(run), repo=str(repo),
                source_run_id=34016397157, source_run_attempt=1, request_id="b" * 64,
                parent_run_id=33991433362, parent_run_attempt=1,
                snapshot_sha256="c" * 64, sefaria_tag="tag-1",
                sefaria_metadata_sha256="d" * 64,
            )
            cache.save(args)  # the strict allow-list must not reject the run dir
            saved = cache_root / ("b" * 64) / "source-34016397157-1"
            manifest = json.loads((saved / "checkpoint_manifest.json").read_text())
            members = [item["path"] for item in manifest["files"]]
            self.assertNotIn("memory/journal.jsonl", members)
            self.assertFalse([m for m in members if m.startswith("memory/")])
            self.assertFalse((saved / "memory").exists())
            self.assertEqual(
                json.loads((saved / "completed_books.json").read_text()), [],
                "a requeued book must never be checkpointed as completed",
            )

            # Restore into a clean run dir, then apply the driver's ledger wipe: the
            # marker must not survive into the next invocation and deny the book the
            # retry it has not had there.
            fresh = root / "fresh-run"
            args.run_dir = str(fresh)
            cache.restore(args)
            self.assertFalse((fresh / "memory").exists())
            self.assertFalse((fresh / "done").exists())
            import shutil
            (fresh / link_books.MEMORY_DIR).mkdir(parents=True)
            for name in ("done", "claim", "failed", "heavy", "heavy-slots",
                         link_books.CLAIM_EVENT_DIR, link_books.MEMORY_DIR):
                shutil.rmtree(fresh / name, ignore_errors=True)
            self.assertFalse((fresh / link_books.MEMORY_DIR).exists())
            self.assertTrue(link_books.claim_memory_requeue(str(fresh), cid, {})[0],
                            "the new invocation must grant the book its own retry")


if __name__ == "__main__":
    unittest.main()
