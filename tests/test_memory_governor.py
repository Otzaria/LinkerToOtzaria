"""Pool memory governor: the host-wide guard added after 36112302819 took the host down.

Policy with injected readers, the child-side abort, the master's accounting for a
killed worker, and two real forks through run_pool.
"""
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

try:  # pragma: no cover - platform gate
    import link_books
except ImportError:  # Windows: POSIX-only `resource`/`fcntl`
    link_books = None

ENGINE = unittest.skipIf(link_books is None, "link_books is POSIX-only")
GB = 1_000_000_000


class _Clock:
    def __init__(self):
        self.available = 40 * GB
        self.private = {}
        self.sent = []
        self.messages = []

    def governor(self, **overrides):
        options = dict(
            log=self.messages.append, soft_floor=10 * GB, hard_floor=6 * GB,
            min_victim=2 * GB, grace=20, kill_interval=2,
            read_available=lambda: self.available,
            read_private=lambda pid: self.private.get(pid),
            send_signal=lambda pid, sig: self.sent.append((pid, sig)),
        )
        options.update(overrides)
        return link_books.PoolMemoryGovernor(**options)


@ENGINE
class GovernorPolicyTest(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.clock.private = {101: 1 * GB, 102: 23 * GB, 103: 5 * GB}
        self.active = {"w01": (101, 0), "w02": (102, 0), "w03": (103, 0)}

    def test_a_healthy_host_is_never_touched(self):
        governor = self.clock.governor()
        governor.tick(self.active, 100)
        self.assertEqual(self.clock.sent, [])

    def test_below_the_soft_floor_only_the_largest_worker_is_asked_once(self):
        self.clock.available = 8 * GB
        governor = self.clock.governor()
        governor.tick(self.active, 100)
        governor.tick(self.active, 105)  # within the grace: nobody else is asked
        self.assertEqual(self.clock.sent, [(102, signal.SIGUSR1)])

    def test_a_worker_still_alive_after_the_grace_is_killed(self):
        self.clock.available = 8 * GB
        governor = self.clock.governor()
        governor.tick(self.active, 100)
        governor.tick(self.active, 121)
        self.assertEqual(self.clock.sent, [(102, signal.SIGUSR1), (102, signal.SIGKILL)])
        self.assertEqual(governor.was_killed(102), 23 * GB)
        self.assertIsNone(governor.was_killed(102), "consumed once")

    def test_below_the_hard_floor_the_largest_is_killed_at_once_and_kills_are_paced(self):
        self.clock.available = 3 * GB
        governor = self.clock.governor()
        governor.tick(self.active, 100)
        governor.tick(self.active, 101)  # inside the kill interval
        self.assertEqual(self.clock.sent, [(102, signal.SIGKILL)])
        governor.tick(self.active, 103)  # the killed pid is skipped; next largest
        self.assertEqual(self.clock.sent[-1], (103, signal.SIGKILL))

    def test_the_request_is_repeated_once_at_half_the_grace(self):
        self.clock.available = 8 * GB
        governor = self.clock.governor()
        for now in (100, 111, 115, 119):
            governor.tick(self.active, now)
        self.assertEqual(self.clock.sent, [(102, signal.SIGUSR1), (102, signal.SIGUSR1)])

    def test_a_recovered_host_forgets_old_requests(self):
        self.clock.available = 8 * GB
        governor = self.clock.governor()
        governor.tick(self.active, 100)
        self.clock.available = 40 * GB
        governor.tick(self.active, 105)
        self.clock.available = 8 * GB
        governor.tick(self.active, 700)  # a new dip: asked again, not killed
        self.assertEqual(self.clock.sent, [(102, signal.SIGUSR1), (102, signal.SIGUSR1)])

    def test_small_workers_are_never_victims(self):
        self.clock.available = 3 * GB
        self.clock.private = {101: 1 * GB, 102: 1.5 * GB}
        governor = self.clock.governor()
        governor.tick({"w01": (101, 0), "w02": (102, 0)}, 100)
        self.assertEqual(self.clock.sent, [])
        self.assertTrue(any("not intervening" in m for m in self.clock.messages))

    def test_a_reaped_worker_is_forgotten_so_the_next_one_can_be_asked(self):
        self.clock.available = 8 * GB
        governor = self.clock.governor()
        governor.tick(self.active, 100)
        governor.forget(102)
        del self.active["w02"]
        governor.tick(self.active, 101)
        self.assertEqual(self.clock.sent[-1], (103, signal.SIGUSR1))

    def test_floors_fall_back_to_fractions_of_memtotal(self):
        with mock.patch.object(link_books, "POOL_MEMORY_SOFT_FLOOR_BYTES", 0), \
                mock.patch.object(link_books, "POOL_MEMORY_HARD_FLOOR_BYTES", 0):
            soft, hard = link_books.pool_memory_floors(total=55 * GB)
        self.assertAlmostEqual(soft, 55 * GB * 0.18)
        self.assertAlmostEqual(hard, 55 * GB * 0.10)
        with mock.patch.object(link_books, "POOL_MEMORY_SOFT_FLOOR_BYTES", 4 * GB), \
                mock.patch.object(link_books, "POOL_MEMORY_HARD_FLOOR_BYTES", 9 * GB):
            self.assertEqual(link_books.pool_memory_floors(total=55 * GB), (4 * GB, 4 * GB))


@ENGINE
class ChildAbortTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = self.temp.name
        link_books._MEMORY_RECYCLE_REQUESTED = False

    def tearDown(self):
        link_books._MEMORY_RECYCLE_REQUESTED = False
        self.temp.cleanup()

    def test_inside_the_window_the_signal_aborts_the_book(self):
        record = {"claim_id": "c" * 40, "source_name": "Sefaria",
                  "canonical_he_title": "t", "heavy": True}
        with self.assertRaises(link_books.HostMemoryPressure):
            with link_books.memory_pressure_window(self.run, "w01", record):
                self.assertEqual(link_books.read_current_book(self.run, "w01")["claim_id"],
                                 "c" * 40)
                link_books._on_memory_pressure(signal.SIGUSR1, None)
        # Kept until the book loop is done with the book, so a kill is still charged.
        self.assertEqual(link_books.read_current_book(self.run, "w01")["claim_id"], "c" * 40)
        link_books.clear_current_book(self.run, "w01")
        self.assertIsNone(link_books.read_current_book(self.run, "w01"))
        self.assertFalse(link_books._MEMORY_WINDOW)
        self.assertTrue(link_books._MEMORY_RECYCLE_REQUESTED)

    def test_outside_the_window_the_signal_only_requests_a_recycle(self):
        link_books._on_memory_pressure(signal.SIGUSR1, None)
        self.assertTrue(link_books._MEMORY_RECYCLE_REQUESTED)

    def test_a_request_pending_at_window_entry_aborts_at_once(self):
        link_books._MEMORY_RECYCLE_REQUESTED = True
        with self.assertRaises(link_books.HostMemoryPressure):
            with link_books.memory_pressure_window(self.run, "w01", {}):
                self.fail("the book must not start")
        self.assertFalse(link_books._MEMORY_WINDOW)

    def test_after_the_window_closes_a_signal_cannot_abort_the_assembly(self):
        with link_books.memory_pressure_window(self.run, "w01", {}):
            link_books.close_memory_window()
            link_books._on_memory_pressure(signal.SIGUSR1, None)  # no raise
        self.assertTrue(link_books._MEMORY_RECYCLE_REQUESTED)

    def test_the_abort_is_a_memory_error_and_escapes_except_exception(self):
        def resolver():
            try:
                with link_books.memory_pressure_window(self.run, "w01", {}):
                    link_books._on_memory_pressure(signal.SIGUSR1, None)
            except Exception:  # Sefaria-style broad handler
                self.fail("the abort must not be swallowed by except Exception")

        try:  # not assertRaises: it clears the traceback the site is read from
            resolver()
        except link_books.HostMemoryPressure as error:
            caught = error
        else:
            self.fail("the abort did not propagate")
        wrapped = RuntimeError("line 4 failed to link")
        wrapped.__cause__ = caught
        self.assertTrue(link_books.is_memory_error(wrapped))
        site = link_books.memory_error_site(wrapped)
        self.assertTrue(any(":resolver:" in frame for frame in site), site)
        self.assertFalse(any("_on_memory_pressure" in frame for frame in site))

    def test_the_site_of_a_real_memory_error_is_its_innermost_frame(self):
        def allocate():
            raise MemoryError()

        try:
            try:
                allocate()
            except MemoryError as error:
                raise RuntimeError("line 43 failed to link: MemoryError:") from error
        except RuntimeError as wrapped:
            site = link_books.memory_error_site(wrapped)
        self.assertIn(":allocate:", site[-1])
        self.assertTrue(site[-1].startswith("tests/test_memory_governor.py"))


@ENGINE
class GovernorKillAccountingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = self.temp.name
        os.makedirs(os.path.join(self.run, "done"))
        os.makedirs(os.path.join(self.run, "claim"))
        self.book = link_books.BookKey("Sefaria", "t")
        self.cid = link_books.claim_id(self.book)
        self.messages = []

    def tearDown(self):
        self.temp.cleanup()

    def _publish(self, heavy):
        path = link_books.current_book_path(self.run, "w03")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"claim_id": self.cid, **self.book.to_dict(), "heavy": heavy,
                       "processed": 0, "limits": {}}, fh)

    def _kill(self):
        link_books.account_governor_kill(self.run, "w03", 21 * GB, self.messages.append)
        self.assertIsNone(link_books.read_current_book(self.run, "w03"))

    def test_a_normal_phase_kill_defers_the_book(self):
        self._publish(heavy=False)
        self._kill()
        self.assertTrue(os.path.exists(link_books.heavy_marker_path(self.run, self.cid)))
        self.assertFalse(os.path.exists(os.path.join(self.run, "done", self.cid)))

    def test_heavy_kills_are_retried_up_to_the_limit_and_serialize_the_heavy_phase(self):
        messages = self.messages
        with mock.patch.object(link_books, "MEMORY_RETRY_LIMIT", 3):
            for _ in range(3):
                self._publish(heavy=True)
                self._kill()
                self.assertFalse(os.path.exists(os.path.join(self.run, "done", self.cid)),
                                 "a governor stop is outside pressure, not a standstill")
                self.assertTrue(link_books.single_heavy_mode(self.run))
            self._publish(heavy=True)
            self._kill()  # the retry limit still bounds it
        self.assertTrue(os.path.exists(os.path.join(self.run, "failed", self.cid)))
        self.assertTrue(os.path.exists(os.path.join(self.run, "done", self.cid)))
        with open(os.path.join(self.run, "failed", self.cid), encoding="utf-8") as fh:
            self.assertIn("MemoryError on 4 attempt(s)", json.load(fh)["note"])
        events = [e["event"] for e in link_books.read_memory_journal(self.run)]
        self.assertEqual(events.count("killed-by-memory-governor"), 4)
        self.assertEqual(events.count("requeued-after-memoryerror"), 3)
        self.assertEqual(events.count("failed-after-requeue"), 1)
        self.assertFalse(link_books.single_heavy_mode(self.run), "ends with the stopped book")
        self.assertEqual(sum("one at a time" in m for m in messages), 1)

    def test_a_kill_is_charged_even_when_a_peer_already_resumed_the_book(self):
        self._publish(heavy=True)
        claim = link_books.BookClaim.acquire(self.run, self.cid)
        try:
            self._kill()
        finally:
            claim.release()
        self.assertEqual(len(link_books.read_memory_retries(self.run, self.cid)), 1)
        self.assertFalse(os.path.exists(os.path.join(self.run, "done", self.cid)),
                         "never mark done a book a live peer is resolving")

    def test_a_refused_charge_under_a_peer_writes_failed_but_not_done(self):
        with mock.patch.object(link_books, "MEMORY_RETRY_LIMIT", 1):
            self._publish(heavy=True)
            self._kill()
            self._publish(heavy=True)
            claim = link_books.BookClaim.acquire(self.run, self.cid)
            try:
                self._kill()
            finally:
                claim.release()
        self.assertTrue(os.path.exists(os.path.join(self.run, "failed", self.cid)))
        self.assertFalse(os.path.exists(os.path.join(self.run, "done", self.cid)))

    def test_a_kill_between_books_accounts_nothing(self):
        self._kill()
        self.assertEqual([e["event"] for e in link_books.read_memory_journal(self.run)],
                         ["killed-by-memory-governor"])
        self.assertFalse(os.path.exists(os.path.join(self.run, "failed")))

    def test_the_published_book_is_not_a_checkpoint_member(self):
        import argparse
        from pathlib import Path

        from ci import local_checkpoint_cache as cache

        root = Path(self.run)
        run, cache_root = root / "run", root / "cache"
        book = {"source_name": "Sefaria", "canonical_he_title": "t", "hash": "1" * 16,
                "ner_ranges": [[0, 1]], "reuse": []}
        (run / "checkpoints" / self.cid).mkdir(parents=True)
        (run / "changed_books.json").write_text(json.dumps([book]), encoding="utf-8")
        (run / "checkpoints" / self.cid / "000000000000.jsonl").write_text(
            '{"record":"exact"}\n', encoding="utf-8")
        with link_books.memory_pressure_window(str(run), "w03", {"claim_id": self.cid}):
            cache.save(argparse.Namespace(
                cache_root=str(cache_root), run_dir=str(run), repo=str(root / "repo"),
                source_run_id=1, source_run_attempt=1, request_id="b" * 64,
                parent_run_id=2, parent_run_attempt=1, snapshot_sha256="c" * 64,
                sefaria_tag="tag-1", sefaria_metadata_sha256="d" * 64,
            ))
        manifest = json.loads(
            (cache_root / ("b" * 64) / "source-1-1" / "checkpoint_manifest.json").read_text())
        self.assertFalse([m["path"] for m in manifest["files"]
                          if m["path"].startswith("memory/")])


@ENGINE
@unittest.skipUnless(hasattr(os, "fork"), "run_pool forks; POSIX only")
class GovernorThroughTheRealPoolTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = os.path.join(self.temp.name, "run")
        os.makedirs(os.path.join(self.run, "worker-heartbeats"))
        self.clock = _Clock()
        self.clock.available = 1 * GB

    def tearDown(self):
        self.temp.cleanup()

    def _governor(self, **overrides):
        # A victim only once the child is inside its window (the ready file).
        ready = os.path.join(self.temp.name, "ready")
        return self.clock.governor(
            read_private=lambda pid: 5 * GB if os.path.exists(ready) else 0,
            send_signal=os.kill, **overrides)

    def _ready(self):
        open(os.path.join(self.temp.name, "ready"), "w").close()

    def test_a_forked_child_gets_the_abort_and_ends_its_life_cleanly(self):
        marker = os.path.join(self.temp.name, "aborted")

        def body():
            try:
                with link_books.memory_pressure_window(self.run, "w01", {}):
                    self._ready()
                    deadline = time.time() + 20
                    while time.time() < deadline:
                        time.sleep(0.05)
            except link_books.HostMemoryPressure:
                open(marker, "w").close()
            return 0

        governor = self._governor(hard_floor=0.5 * GB, grace=30)
        code = link_books.run_pool(
            1, self.run, "pool", lambda label: link_books.install_memory_pressure_handler(),
            body, log=lambda m: None, poll_seconds=0.05, governor=governor)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(marker))

    def test_children_ignore_the_request_until_they_install_the_handler(self):
        def body():
            self._ready()
            deadline = time.time() + 1.5
            while time.time() < deadline:
                time.sleep(0.05)
            return 0

        governor = self._governor(hard_floor=0.5 * GB, grace=30)
        code = link_books.run_pool(1, self.run, "pool", lambda label: None, body,
                                   restart_limit=0, log=lambda m: None,
                                   poll_seconds=0.05, governor=governor)
        self.assertEqual(code, 0)
        with open(os.path.join(self.run, "logs", "pool-exit-codes.json")) as fh:
            self.assertEqual(json.load(fh), {"w01": [0]}, "SIGUSR1 must not kill it")
        self.assertIsNot(signal.getsignal(signal.SIGUSR1), signal.SIG_IGN,
                         "the master restores its own disposition")

    def test_a_killed_child_is_reforked_without_spending_a_restart(self):
        lives = os.path.join(self.temp.name, "lives")
        book = link_books.BookKey("Sefaria", "t")
        cid = link_books.claim_id(book)

        def body():
            with open(lives, "a") as fh:
                fh.write("x")
            if os.path.getsize(lives) > 1:
                os.remove(os.path.join(self.temp.name, "ready"))
                return 0  # the replacement finds nothing to do
            record = {"claim_id": cid, **book.to_dict(), "heavy": False,
                      "processed": 0, "limits": {}}
            with link_books.memory_pressure_window(self.run, "w01", record):
                self._ready()
                time.sleep(30)
            return 0

        os.makedirs(os.path.join(self.run, "claim"), exist_ok=True)
        governor = self._governor(hard_floor=2 * GB)
        messages = []
        code = link_books.run_pool(1, self.run, "pool", lambda label: None, body,
                                   restart_limit=0, log=messages.append,
                                   poll_seconds=0.05, governor=governor)
        self.assertEqual(code, 0, messages)
        with open(lives) as fh:
            self.assertEqual(fh.read(), "xx")
        self.assertTrue(os.path.exists(link_books.heavy_marker_path(self.run, cid)))
        with open(os.path.join(self.run, "logs", "pool-exit-codes.json")) as fh:
            self.assertEqual(json.load(fh), {"w01": [0]})


@ENGINE
class CommitGateTest(unittest.TestCase):
    """Nothing computed after an abort or MemoryError is committed, even if Sefaria
    swallows it (a bare ``except:`` there drops a citation silently)."""

    def setUp(self):
        import test_ibid_checkpoint as ibid
        self.ibid = ibid
        self.temp = tempfile.TemporaryDirectory()
        self.checkpoint = os.path.join(self.temp.name, "ck")
        self.output = os.path.join(self.temp.name, "out.jsonl")
        self._reset()

    def tearDown(self):
        self._reset()
        self.temp.cleanup()

    @staticmethod
    def _reset():
        link_books._IMAGE_TAINTED = False
        link_books._MEMORY_RECYCLE_REQUESTED = False
        link_books._MEMORY_WINDOW = False

    def _linker(self, trouble_on_line):
        ibid = self.ibid

        class Swallowing(ibid._Linker):
            def bulk_link(inner, texts, book_context_refs=None, type_filter=None):
                inner._ref_resolver.reset_ibid_history()
                docs = []
                for text in texts:
                    ref = inner._instantiate(text)
                    found = []
                    if ref is not None:
                        inner._ref_resolver._ibid_history.last_refs = ref
                        found = [ibid._ResolvedRef(ref)]
                    docs.append(type("Doc", (), {"resolved_refs": found})())
                return docs

            def _instantiate(inner, text):
                try:  # the shape of Sefaria's resolve_raw_ref_using_ref_instantiation
                    if text.split()[1] == str(trouble_on_line):
                        self.trouble()
                    return ibid.Ref(text)
                except:  # noqa: E722 - exactly the pinned Sefaria code
                    return None

        return Swallowing()

    def _shards(self):
        return sorted(name for name in os.listdir(self.checkpoint) if name.endswith(".jsonl"))

    def test_an_untroubled_book_is_unchanged(self):
        self.trouble = lambda: None
        with link_books.memory_pressure_window(self.temp.name, "w01", {}):
            self.ibid._run(self._linker(3), self.checkpoint, self.output)
        from linker_artifact import read_artifact
        self.assertEqual(sorted(r.line_index for r in read_artifact(self.output)),
                         [0, 1, 2, 3, 4, 5])

    def test_a_swallowed_abort_commits_nothing_after_it(self):
        self.trouble = lambda: link_books._on_memory_pressure(signal.SIGUSR1, None)
        with self.assertRaises(link_books.HostMemoryPressure):
            with link_books.memory_pressure_window(self.temp.name, "w01", {}):
                self.ibid._run(self._linker(3), self.checkpoint, self.output)
        self.assertEqual(self._shards(), ["000000000000.jsonl"],
                         "the batch that lost its citation must not be committed")
        self.assertFalse(os.path.exists(self.output))

    @unittest.skipUnless(hasattr(sys, "monitoring"), "sys.monitoring needs Python 3.12+")
    def test_a_swallowed_real_memory_error_is_gated_too(self):
        def allocate():
            bytearray(1 << 62)  # MemoryError raised in C, then swallowed

        self.trouble = allocate
        self.assertTrue(link_books.install_memory_taint_monitor())
        try:
            with self.assertRaises(link_books.HostMemoryPressure):
                self.ibid._run(self._linker(3), self.checkpoint, self.output)
        finally:
            sys.monitoring.set_events(link_books._TAINT_TOOL_ID, 0)
            sys.monitoring.free_tool_id(link_books._TAINT_TOOL_ID)
        self.assertEqual(self._shards(), ["000000000000.jsonl"])
        self.assertFalse(os.path.exists(self.output))


@ENGINE
class RetryPolicyUnderTheGovernorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def test_governor_stops_are_not_standstills(self):
        grants = [link_books.claim_memory_requeue(
            self.run, "c" * 40, {"committed": 5, "governor": True})[0] for _ in range(6)]
        self.assertEqual(grants, [True] * 6)
        # The book's own standstills still end it: two in a row after them.
        self.assertTrue(link_books.claim_memory_requeue(self.run, "c" * 40, {"committed": 5})[0])
        self.assertFalse(link_books.claim_memory_requeue(self.run, "c" * 40, {"committed": 5})[0])

    def test_victims_are_sized_with_their_swap(self):
        text = "Rss: 9 kB\nPrivate_Clean: 1 kB\nPrivate_Dirty: 2 kB\nSwap: 8 kB\nSwapPss: 4 kB\n"
        self.assertEqual(link_books.owned_bytes_from_smaps(text), 7 * 1024)

    def test_single_heavy_mode_lasts_until_the_stopped_book_is_done(self):
        messages = []
        os.makedirs(os.path.join(self.run, "done"))
        self.assertFalse(link_books.single_heavy_mode(self.run))
        link_books.enter_single_heavy_mode(self.run, "a" * 40, messages.append)
        link_books.enter_single_heavy_mode(self.run, "b" * 40, messages.append)
        self.assertTrue(link_books.single_heavy_mode(self.run))
        self.assertEqual(len(messages), 1)
        open(os.path.join(self.run, "done", "a" * 40), "w").close()
        self.assertFalse(link_books.single_heavy_mode(self.run))

    def test_exclusive_slots_take_every_slot_or_none(self):
        one = link_books.HeavySlot.acquire(self.run, 2)
        self.assertIsNone(link_books.HeavySlot.acquire_all(self.run, 2))
        one.release()
        every = link_books.HeavySlot.acquire_all(self.run, 2)
        self.assertIsNotNone(every)
        self.assertIsNone(link_books.HeavySlot.acquire(self.run, 2), "no slot left beside it")
        every.release()
        self.assertIsNotNone(link_books.HeavySlot.acquire(self.run, 2))


if __name__ == "__main__":
    unittest.main()
