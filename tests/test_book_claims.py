"""Exclusive book claims in the resolve pool, and the EAFP artifact retirement.

Audit of cycle 33987355439 (item L4, reports 09/10/11-relink). Two defects, one
cause: in the raw-NER ("cooperative") path the resolver claimed batches but not
books, so every worker that reached a book linked it again.

  * relink 34016397157: 7,274 `done` events for 5,126 distinct books — 2,148
    redundant (29.5%); 1,107 books completed 2-10 times; 268,047 book-lines re-read
    from the snapshot.  relink 33994031370: 1,558 redundant of 6,685 (23.3%).
  * the pile-up on one artifact raced `if os.path.exists(p): os.remove(p)` at
    src/link_books.py:1620 into 36 `FileNotFoundError` worker deaths across the two
    runs, spending 8 of 12 labels' entire bounded restart budget in 33994031370.

These tests pin the repaired protocol: one owner per book, a dead owner's book
re-claimed (never dropped), a rescan that hands out only unclaimed books, and an
idempotent artifact retirement.

Windows: `link_books` imports POSIX-only `resource`/`fcntl`, so the claim tests are
explicitly skipped there (as the repo's other engine tests already are). The EAFP
retirement and the summary line live in modules that import everywhere and are
exercised on every platform.
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

import incremental  # noqa: E402
from linker_artifact import remove_artifact  # noqa: E402

try:  # pragma: no cover - platform gate
    import link_books
except ImportError:  # Windows: link_books imports POSIX-only `resource`
    link_books = None

ENGINE = unittest.skipIf(
    link_books is None,
    "link_books requires POSIX `resource`/`fcntl`; the claim protocol is Linux-only",
)

# A child that takes a claim and holds it until told to stop (or until it is killed).
# Subprocess, never fork: this must also be meaningful on a platform without fork.
HOLDER = """
import os, sys, time
sys.path.insert(0, {src!r})
import link_books

run, cid, ready, stop = sys.argv[1:5]
claim = link_books.BookClaim.acquire(run, cid)
with open(ready, "w", encoding="utf-8") as fh:
    fh.write("won" if claim is not None else "lost")
while not os.path.exists(stop):
    time.sleep(0.02)
if claim is not None:
    claim.release()
"""


def _wait_for(path, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.01)
    return False


class ArtifactRetirementTest(unittest.TestCase):
    """`remove_artifact` — the EAFP replacement for the exists()/remove() pair."""

    def test_absent_artifact_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(remove_artifact(os.path.join(tmp, "never-written.jsonl")))
            # A missing PARENT directory is the same non-event, not an ENOENT crash.
            self.assertFalse(remove_artifact(os.path.join(tmp, "no", "dir", "x.jsonl")))

    def test_present_artifact_is_removed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "book.jsonl")
            open(path, "w").close()
            self.assertTrue(remove_artifact(path))
            self.assertFalse(os.path.exists(path))
            self.assertFalse(remove_artifact(path))

    @unittest.skipIf(
        os.name == "nt",
        "Windows reports a concurrent unlink of a delete-pending file as "
        "ERROR_ACCESS_DENIED, not ENOENT; the ENOENT branch that killed the "
        "resolvers is covered portably by test_absent_artifact_is_not_an_error",
    )
    def test_concurrent_retirement_never_raises_and_has_one_winner(self):
        # The 2026-09-06 crash shape: several resolvers retire the same zero-link
        # book at once.  unlink() is atomic, so exactly one caller may report the
        # removal and the losers must see the ENOENT non-event, not a traceback.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "empty-book.jsonl")
            for _round in range(200):
                open(path, "w").close()
                start = threading.Barrier(8)
                removed = []
                errors = []

                def retire():
                    try:
                        start.wait()
                        if remove_artifact(path):
                            removed.append(1)
                    except BaseException as error:  # noqa: BLE001 - the point of the test
                        errors.append(error)

                threads = [threading.Thread(target=retire) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                self.assertEqual(errors, [], "EAFP retirement raised under contention")
                # The exclusive winner is a kernel guarantee production relies on
                # nowhere: macOS/APFS has been reported to hand several callers a
                # successful unlink of the same file (which is gone exactly once
                # either way), so assert it only where the linker actually runs.
                # `errors == []` and the post-condition stay unconditional — they
                # are what the 2026-09-06 FileNotFoundError crash would trip.
                if sys.platform.startswith("linux"):
                    self.assertEqual(
                        len(removed), 1, "more than one caller claimed the removal"
                    )
                self.assertFalse(os.path.exists(path))


class ClaimSummaryTest(unittest.TestCase):
    def test_summary_names_claims_recoveries_and_duplicates(self):
        self.assertEqual(
            incremental.format_claim_summary(claimed=5127, recovered=3, duplicates=0),
            "claims: books claimed 5127, re-claimed after worker death 3, duplicates 0",
        )

    def test_summary_reports_a_regression_rather_than_hiding_it(self):
        self.assertIn(
            "duplicates 2148",
            incremental.format_claim_summary(claimed=5126, recovered=0, duplicates=2148),
        )


@ENGINE
class BookClaimTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = os.path.join(self.temp.name, "run")
        os.makedirs(os.path.join(self.run, "done"))
        self.cid = "a" * 40

    def tearDown(self):
        self.temp.cleanup()

    def _spawn_holder(self, cid=None, name="holder"):
        cid = cid or self.cid
        script = os.path.join(self.temp.name, f"{name}.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(HOLDER.format(src=os.path.join(ROOT, "src")))
        ready = os.path.join(self.temp.name, f"{name}.ready")
        stop = os.path.join(self.temp.name, f"{name}.stop")
        proc = subprocess.Popen([sys.executable, script, self.run, cid, ready, stop])
        self.assertTrue(_wait_for(ready), "claim holder never started")
        with open(ready, encoding="utf-8") as fh:
            return proc, fh.read().strip(), stop

    def test_two_threads_racing_one_book_yield_exactly_one_owner(self):
        # flock is owned by the open file description, so two threads racing with
        # their own descriptors exclude each other exactly as two processes would.
        for _round in range(50):
            start = threading.Barrier(8)
            claims = []
            errors = []

            def take():
                try:
                    start.wait()
                    claim = link_books.BookClaim.acquire(self.run, self.cid)
                    if claim is not None:
                        claims.append(claim)
                except BaseException as error:  # noqa: BLE001
                    errors.append(error)

            threads = [threading.Thread(target=take) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(claims), 1, "a book was claimed by more than one worker")
            claims[0].release()

    def test_two_processes_racing_one_book_yield_exactly_one_owner(self):
        first, first_result, first_stop = self._spawn_holder(name="h1")
        try:
            second, second_result, second_stop = self._spawn_holder(name="h2")
            try:
                self.assertEqual(
                    sorted([first_result, second_result]), ["lost", "won"],
                    "two processes both believed they owned the book",
                )
                # The in-process view agrees with the losing peer's.
                self.assertTrue(link_books.BookClaim.is_claimed(self.run, self.cid))
                self.assertIsNone(link_books.BookClaim.acquire(self.run, self.cid))
            finally:
                open(second_stop, "w").close()
                second.wait(timeout=30)
        finally:
            open(first_stop, "w").close()
            first.wait(timeout=30)
        # Both owners released: the book is available again, not stranded.
        self.assertFalse(link_books.BookClaim.is_claimed(self.run, self.cid))
        claim = link_books.BookClaim.acquire(self.run, self.cid)
        self.assertIsNotNone(claim)
        claim.release()

    def test_a_dead_owner_releases_its_book_which_is_then_completed(self):
        proc, result, _stop = self._spawn_holder(name="dead")
        self.assertEqual(result, "won")
        self.assertTrue(link_books.BookClaim.is_claimed(self.run, self.cid))
        self.assertIsNone(link_books.BookClaim.acquire(self.run, self.cid))

        proc.kill()          # no release(), no heartbeat wind-down: the owner just dies
        proc.wait(timeout=30)
        # The kernel drops a dead owner's flock, so the takeover is immediate — the
        # 15-minute heartbeat staleness window would otherwise strand the book.
        deadline = time.time() + 30
        while link_books.BookClaim.is_claimed(self.run, self.cid) and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(link_books.BookClaim.is_claimed(self.run, self.cid))

        claim = link_books.BookClaim.acquire(self.run, self.cid)
        self.assertIsNotNone(claim, "a dead worker's book was never re-claimed")
        self.assertEqual(link_books.count_claim_events(self.run, "recovered"), 1)
        self.assertEqual(link_books.count_claim_events(self.run, "duplicate"), 0)
        # …and the replacement can finish it: the book is retried, never dropped.
        self.assertTrue(link_books.mark_done(self.run, self.cid))
        claim.release()
        self.assertTrue(os.path.isfile(os.path.join(self.run, "done", self.cid)))

    def test_a_book_already_done_is_never_claimed_again(self):
        link_books.mark_done(self.run, self.cid)
        self.assertIsNone(link_books.BookClaim.acquire(self.run, self.cid))

    def test_release_is_idempotent_so_every_loop_exit_may_release(self):
        # The book loop releases on the success/failure/outage paths AND again in its
        # finally, because the BookWorkInProgress path leaves the book unfinished: a
        # claim held past that break would hide the book from every peer's rescan
        # while this worker never returns to it.
        claim = link_books.BookClaim.acquire(self.run, self.cid)
        claim.release()
        claim.release()
        self.assertFalse(link_books.BookClaim.is_claimed(self.run, self.cid))
        again = link_books.BookClaim.acquire(self.run, self.cid)
        self.assertIsNotNone(again, "an unfinished book was not offered to a peer")
        again.release()

    def test_heartbeat_after_release_is_refused(self):
        claim = link_books.BookClaim.acquire(self.run, self.cid)
        claim.release()
        with self.assertRaises(RuntimeError):
            claim.heartbeat()


@ENGINE
class RescanHandoutTest(unittest.TestCase):
    """`unclaimed()` — the set the rescan is allowed to hand a worker."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = os.path.join(self.temp.name, "run")
        os.makedirs(os.path.join(self.run, "done"))
        self.books = [link_books.BookKey("Sefaria", f"ספר {n}") for n in range(4)]

    def tearDown(self):
        self.temp.cleanup()

    def test_a_claimed_book_is_withheld_and_returns_when_released(self):
        held = link_books.BookClaim.acquire(self.run, link_books.claim_id(self.books[1]))
        self.assertIsNotNone(held)
        self.assertEqual(
            link_books.unclaimed(self.books, self.run),
            [self.books[0], self.books[2], self.books[3]],
        )
        held.release()
        self.assertEqual(link_books.unclaimed(self.books, self.run), self.books)

    def test_rescan_hands_out_pending_unclaimed_books_in_plan_order(self):
        # done → out of the pending set entirely; heavy → deferred to the heavy
        # phase; claimed → withheld this round. What is left is what a worker gets.
        link_books.mark_done(self.run, link_books.claim_id(self.books[0]))
        link_books.mark_heavy(
            self.run, link_books.claim_id(self.books[2]), self.books[2], 1, "test"
        )
        held = link_books.BookClaim.acquire(self.run, link_books.claim_id(self.books[1]))
        try:
            normal, heavy = link_books.partition_pending(self.books, self.run)
            self.assertEqual(normal, [self.books[1], self.books[3]])
            self.assertEqual(heavy, [self.books[2]])
            self.assertEqual(link_books.unclaimed(normal, self.run), [self.books[3]])
            # The pending list itself is unchanged: the heavy phase must still wait
            # for every ordinary book to be DONE, not merely claimed.
            self.assertEqual(link_books.unclaimed(heavy, self.run), [self.books[2]])
        finally:
            held.release()

    def test_a_book_whose_owner_died_is_offered_again(self):
        cid = link_books.claim_id(self.books[2])
        script = os.path.join(self.temp.name, "holder.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(HOLDER.format(src=os.path.join(ROOT, "src")))
        ready = os.path.join(self.temp.name, "holder.ready")
        proc = subprocess.Popen([
            sys.executable, script, self.run, cid, ready,
            os.path.join(self.temp.name, "never"),
        ])
        try:
            self.assertTrue(_wait_for(ready))
            self.assertNotIn(self.books[2], link_books.unclaimed(self.books, self.run))
        finally:
            proc.kill()
            proc.wait(timeout=30)
        deadline = time.time() + 30
        while (self.books[2] not in link_books.unclaimed(self.books, self.run)
               and time.time() < deadline):
            time.sleep(0.02)
        self.assertIn(self.books[2], link_books.unclaimed(self.books, self.run),
                      "a dead worker's book was withheld from the rescan forever")


@ENGINE
class ProtocolUnderConcurrencyTest(unittest.TestCase):
    """The primitives composed the way the resolve loop composes them.

    Twelve resolvers walking one ordered plan is the exact shape that produced 2,148
    redundant completions.  This drives the same sequence the book loop runs —
    partition_pending → unclaimed → BookClaim.acquire → mark_done → release — and
    demands the three properties the run depends on: every book completed, completed
    exactly once, and the whole thing terminating (no worker starved by its peers'
    probes, no book withheld forever).
    """

    def test_twelve_resolvers_complete_every_book_exactly_once(self):
        books = [link_books.BookKey("Sefaria", f"ספר {n}") for n in range(200)]
        with tempfile.TemporaryDirectory() as tmp:
            run = os.path.join(tmp, "run")
            os.makedirs(os.path.join(run, "done"))
            completions = []
            errors = []
            lock = threading.Lock()
            start = threading.Barrier(12)
            deadline = time.time() + 120

            def resolver(label):
                try:
                    start.wait()
                    while time.time() < deadline:
                        pending, _heavy = link_books.partition_pending(books, run)
                        if not pending:
                            return
                        for book in link_books.unclaimed(pending, run):
                            cid = link_books.claim_id(book)
                            claim = link_books.BookClaim.acquire(run, cid)
                            if claim is None:
                                continue
                            try:
                                if link_books.mark_done(run, cid):
                                    with lock:
                                        completions.append((cid, label))
                            finally:
                                claim.release()
                    errors.append(RuntimeError(f"{label} did not finish in time"))
                except BaseException as error:  # noqa: BLE001
                    errors.append(error)

            threads = [threading.Thread(target=resolver, args=(f"w{n:02d}",))
                       for n in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=180)
            self.assertEqual(errors, [])
            self.assertFalse([t for t in threads if t.is_alive()], "a resolver hung")

            expected = {link_books.claim_id(book) for book in books}
            self.assertEqual({cid for cid, _ in completions}, expected,
                             "a book was dropped")
            self.assertEqual(len(completions), len(expected),
                             "a book was completed more than once")
            self.assertEqual(link_books.count_claim_events(run, "duplicate"), 0)
            self.assertEqual(link_books.count_claim_events(run, "recovered"), 0)
            self.assertEqual(len(os.listdir(os.path.join(run, "done"))), len(expected))
            # Work actually spread: an accidental global lock would let one thread do
            # everything and still satisfy every assertion above.
            self.assertGreater(len({label for _cid, label in completions}), 1)


@ENGINE
class DoneMarkerTest(unittest.TestCase):
    """`mark_done()` — the exact file checkpoint adoption reads, created exclusively."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = os.path.join(self.temp.name, "run")
        self.cid = "b" * 40

    def tearDown(self):
        self.temp.cleanup()

    def test_layout_is_an_empty_regular_file_named_by_claim_id(self):
        self.assertTrue(link_books.mark_done(self.run, self.cid))
        path = os.path.join(self.run, "done", self.cid)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.path.getsize(path), 0)
        self.assertEqual(os.listdir(os.path.join(self.run, "done")), [self.cid])

    def test_second_completion_is_refused_and_counted(self):
        self.assertTrue(link_books.mark_done(self.run, self.cid))
        self.assertFalse(link_books.mark_done(self.run, self.cid))
        self.assertEqual(link_books.count_claim_events(self.run, "duplicate"), 1)
        # Counting is per book, not per attempt: a third try adds no new book.
        self.assertFalse(link_books.mark_done(self.run, self.cid))
        self.assertEqual(link_books.count_claim_events(self.run, "duplicate"), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.run, "done", self.cid)))


@ENGINE
class CheckpointAdoptionLayoutTest(unittest.TestCase):
    """A run whose done markers came from mark_done is still adoptable."""

    def test_checkpoint_save_accepts_mark_done_markers(self):
        import argparse
        import json
        from pathlib import Path

        from ci import local_checkpoint_cache as cache

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, repo, cache_root = root / "run", root / "repo", root / "cache"
            book = {
                "source_name": "Source",
                "canonical_he_title": "Book",
                "hash": "1" * 16,
                "ner_ranges": [[0, 1]],
                "reuse": [],
            }
            cid = link_books.claim_id(link_books.BookKey("Source", "Book"))
            (run / "checkpoints" / cid).mkdir(parents=True)
            (run / "changed_books.json").write_text(json.dumps([book]), encoding="utf-8")
            (run / "checkpoints" / cid / "000000000000.jsonl").write_text(
                '{"record":"exact"}\n', encoding="utf-8")
            artifact = repo / "artifacts" / "Source" / "Book.jsonl"
            artifact.parent.mkdir(parents=True)
            artifact.write_text('{"record":"complete"}\n', encoding="utf-8")

            self.assertTrue(link_books.mark_done(str(run), cid))
            # claim-events/ is run-scoped bookkeeping, never checkpoint state: it must
            # not reach the saver's allow-list.
            link_books.record_claim_event(str(run), "recovered", cid)

            args = argparse.Namespace(
                cache_root=str(cache_root), run_dir=str(run), repo=str(repo),
                source_run_id=123, source_run_attempt=1, request_id="b" * 64,
                parent_run_id=456, parent_run_attempt=2, snapshot_sha256="c" * 64,
                sefaria_tag="tag-1", sefaria_metadata_sha256="d" * 64,
            )
            cache.save(args)
            saved = json.loads(
                (cache_root / ("b" * 64) / "source-123-1" / "completed_books.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(
                saved, [{"claim_id": cid, "source_name": "Source",
                         "canonical_he_title": "Book", "hash": "1" * 16,
                         "artifact": f"{cid}.jsonl"}])


if __name__ == "__main__":
    unittest.main()
