"""The shared-library resolver pool (link_books.run_pool) and its driver wiring.

The pool exists so the ~2 GB Sefaria library is loaded ONCE per relink and shared
copy-on-write by forked resolver children, instead of N independent loads that
exhausted the WSL host. These tests drive run_pool with tiny in-process child
bodies (no Sefaria) and pin the supervisor contract it must reproduce: per-label
heartbeats, stall kill + bounded replacement, recycle re-fork, exit-code ledger.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

CAN_FORK = hasattr(os, "fork")


def _touch(path):
    open(path, "a").close()
    os.utime(path, None)


@unittest.skipUnless(CAN_FORK, "run_pool forks; POSIX only")
class RunPoolTest(unittest.TestCase):
    def setUp(self):
        import link_books
        self.link_books = link_books
        self.tmp = tempfile.mkdtemp()
        self.run_dir = os.path.join(self.tmp, "run")
        os.makedirs(os.path.join(self.run_dir, "worker-heartbeats"))
        self.messages = []

    def _log(self, message):
        self.messages.append(message)

    def _lives(self, label):
        path = os.path.join(self.tmp, f"lives-{label}")
        count = 1
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                count = int(fh.read().strip()) + 1
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(count))
        return count

    def _codes(self):
        with open(os.path.join(self.run_dir, "logs", "pool-exit-codes.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_forks_one_child_per_label_and_children_see_the_pool_flag(self):
        lb = self.link_books
        seen = {}

        def setup(label):
            seen["label"] = label

        def body():
            # Only the child sees _POOL_CHILD; the parent's module state is untouched.
            with open(os.path.join(self.tmp, f"child-{seen['label']}"), "w", encoding="utf-8") as fh:
                fh.write(f"{os.getpid()} {lb._POOL_CHILD}")
            _touch(os.path.join(self.run_dir, "worker-heartbeats", seen["label"]))
            return 0

        code = lb.run_pool(3, self.run_dir, "pool", setup, body, log=self._log, poll_seconds=0.05)
        self.assertEqual(code, 0)
        self.assertFalse(lb._POOL_CHILD)
        pids = set()
        for label in ("w01", "w02", "w03"):
            with open(os.path.join(self.tmp, f"child-{label}"), encoding="utf-8") as fh:
                pid, flag = fh.read().split()
            self.assertEqual(flag, "True")
            self.assertNotEqual(int(pid), os.getpid())
            pids.add(pid)
        self.assertEqual(len(pids), 3)
        self.assertEqual(self._codes(), {"w01": [0], "w02": [0], "w03": [0]})
        self.assertTrue(os.path.exists(os.path.join(self.run_dir, "worker-heartbeats", "pool")))

    def test_master_heartbeat_keeps_refreshing_while_a_child_works(self):
        lb = self.link_books
        master_hb = os.path.join(self.run_dir, "worker-heartbeats", "pool")
        stamps = []

        def setup(label):
            pass

        def body():
            time.sleep(1.5)
            return 0

        import threading
        stop = threading.Event()

        def sample():
            while not stop.is_set():
                try:
                    stamps.append(os.path.getmtime(master_hb))
                except OSError:
                    pass
                time.sleep(0.1)

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        try:
            self.assertEqual(lb.run_pool(1, self.run_dir, "pool", setup, body, log=self._log,
                                         poll_seconds=0.05), 0)
        finally:
            stop.set()
            sampler.join()
        # The outer driver's watchdog reads this mtime: it must advance during the run.
        self.assertGreater(max(stamps) - min(stamps), 0.5)

    def test_recycle_exit_reforks_from_the_master_without_counting_as_failure(self):
        lb = self.link_books
        state = {}

        def setup(label):
            state["label"] = label

        def body():
            life = self._lives(state["label"])
            if state["label"] == "w01" and life == 1:
                lb._recycle_process()  # pool child: exits with RECYCLE_EXIT_CODE
                self.fail("recycle must not return")
            return 0

        code = lb.run_pool(2, self.run_dir, "pool", setup, body, restart_limit=0,
                           log=self._log, poll_seconds=0.05)
        self.assertEqual(code, 0)
        with open(os.path.join(self.tmp, "lives-w01"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "2")
        self.assertEqual(self._codes(), {"w01": [0], "w02": [0]})
        self.assertTrue(any("w01 recycled" in m for m in self.messages))

    def test_crash_is_replaced_within_the_bounded_limit_then_reported_unclean(self):
        lb = self.link_books
        state = {}

        def setup(label):
            state["label"] = label

        def body():
            self._lives(state["label"])
            return 3 if state["label"] == "w01" else 0

        code = lb.run_pool(2, self.run_dir, "pool", setup, body, restart_limit=1,
                           log=self._log, poll_seconds=0.05)
        self.assertEqual(code, 1)
        with open(os.path.join(self.tmp, "lives-w01"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "2")  # original + one bounded replacement
        self.assertEqual(self._codes(), {"w01": [3, 3], "w02": [0]})
        self.assertTrue(any("replacement limit 1 exhausted" in m for m in self.messages))

    def test_stalled_child_is_killed_and_replaced(self):
        lb = self.link_books
        state = {}

        def setup(label):
            state["label"] = label

        def body():
            life = self._lives(state["label"])
            if state["label"] == "w01" and life == 1:
                time.sleep(60)  # never heartbeats: the watchdog must kill it
            return 0

        started = time.time()
        code = lb.run_pool(1, self.run_dir, "pool", setup, body, restart_limit=1,
                           stall_seconds=3.0, log=self._log, poll_seconds=0.05)
        self.assertLess(time.time() - started, 20)
        self.assertEqual(code, 0)
        self.assertEqual(self._codes(), {"w01": [-9, 0]})
        self.assertTrue(any("no heartbeat" in m for m in self.messages))

    def test_sigterm_forwarding_stops_children_without_replacement(self):
        # Drive a real master in a subprocess so SIGTERM arrives from outside.
        driver = os.path.join(self.tmp, "driver.py")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(
                "import os, sys, time\n"
                f"sys.path.insert(0, {os.path.join(ROOT, 'src')!r})\n"
                "import link_books\n"
                "def setup(label):\n"
                f"    open(os.path.join({self.tmp!r}, 'child-' + label + '.pid'), 'w').write(str(os.getpid()))\n"
                "def body():\n"
                "    time.sleep(120)\n"
                "    return 0\n"
                f"sys.exit(link_books.run_pool(2, {self.run_dir!r}, 'pool', setup, body, poll_seconds=0.05))\n"
            )
        proc = subprocess.Popen([sys.executable, driver])
        child_pids = []
        try:
            deadline = time.time() + 15
            pid_files = [os.path.join(self.tmp, f"child-w0{n}.pid") for n in (1, 2)]
            while time.time() < deadline and not all(os.path.exists(p) for p in pid_files):
                time.sleep(0.1)
            self.assertTrue(all(os.path.exists(p) for p in pid_files))
            child_pids = [int(open(p, encoding="utf-8").read()) for p in pid_files]
            proc.terminate()
            self.assertEqual(proc.wait(timeout=30), 1)  # children were killed: unclean
            for pid in child_pids:
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            self.assertEqual(self._codes(), {"w01": [-15], "w02": [-15]})
        finally:
            if proc.poll() is None:
                proc.kill()
            for pid in child_pids:
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass


@unittest.skipUnless(sys.platform.startswith("linux"), "smaps_rollup is Linux-specific")
class PrivateBytesTest(unittest.TestCase):
    def test_private_bytes_is_positive_and_never_above_rss(self):
        import link_books
        private = link_books.private_bytes()
        self.assertGreater(private, 0)
        self.assertLessEqual(private, link_books.rss_bytes())


class PoolChildSetupTest(unittest.TestCase):
    def test_child_gets_its_own_connection_and_heartbeat_path(self):
        import sqlite3
        import link_books
        tmp = tempfile.mkdtemp()
        snapshot = os.path.join(tmp, "snap.db")
        sqlite3.connect(snapshot).close()
        with mock.patch.object(link_books, "private_bytes", return_value=1):
            con, hb = link_books.prepare_pool_child(snapshot, os.path.join(tmp, "run"), "w07")
        self.assertIsInstance(con, sqlite3.Connection)
        con.execute("select 1")
        con.close()
        self.assertEqual(hb, os.path.join(tmp, "run", "worker-heartbeats", "w07"))

    def test_child_setup_refuses_a_kernel_without_private_memory_accounting(self):
        import link_books
        with mock.patch.object(link_books, "private_bytes", side_effect=OSError("no smaps_rollup")):
            with self.assertRaisesRegex(RuntimeError, "smaps_rollup"):
                link_books.prepare_pool_child("unused.db", tempfile.mkdtemp(), "w01")


class RecycleActionTest(unittest.TestCase):
    def test_pool_child_recycles_via_exit_code_not_exec(self):
        import link_books
        with mock.patch.object(link_books, "_POOL_CHILD", True), \
                mock.patch.object(link_books.os, "_exit", side_effect=SystemExit("exited")) as exited, \
                mock.patch.object(link_books.os, "execv") as execv:
            with self.assertRaises(SystemExit):
                link_books._recycle_process()
        exited.assert_called_once_with(link_books.RECYCLE_EXIT_CODE)
        execv.assert_not_called()

    def test_standalone_worker_still_execs_itself(self):
        import link_books
        with mock.patch.object(link_books, "_POOL_CHILD", False), \
                mock.patch.object(link_books.os, "execv") as execv:
            link_books._recycle_process()
        execv.assert_called_once()

    def test_memory_probe_follows_the_pool_flag(self):
        import link_books
        with mock.patch.object(link_books, "rss_bytes", return_value=11), \
                mock.patch.object(link_books, "private_bytes", return_value=7):
            with mock.patch.object(link_books, "_POOL_CHILD", False):
                self.assertEqual(link_books.worker_memory_bytes(), 11)
            with mock.patch.object(link_books, "_POOL_CHILD", True):
                self.assertEqual(link_books.worker_memory_bytes(), 7)


class DriverPoolWiringTest(unittest.TestCase):
    def _run(self, run_dir=None, **extra):
        import incremental

        class FakeProc:
            def __init__(self, argv, **kwargs):
                self.argv = argv
                self.kwargs = kwargs
                self.pid = 4242
            def poll(self):
                return 0
            def wait(self, timeout=None):
                return 0

        spawned = []

        def fake_popen(argv, **kwargs):
            proc = FakeProc(argv, **kwargs)
            spawned.append(proc)
            return proc

        args = types.SimpleNamespace(
            python="py", snapshot="s.db", repo="r", run_dir=run_dir or os.path.join(tempfile.mkdtemp(), "run"),
            sef_project="/sef", bavli_convention=False, engine_workers=3,
            engine_restart_limit=2, worker_stall_seconds=1800, **extra,
        )
        # _run_engine imports subprocess lazily; patch the attribute it resolves at call time.
        with mock.patch("subprocess.Popen", side_effect=fake_popen):
            codes = incremental._run_engine(args, os.path.join(args.run_dir, "only.json"))
        return codes, spawned

    def test_pool_mode_spawns_one_master_carrying_the_worker_count(self):
        codes, spawned = self._run(engine_pool=True)
        self.assertEqual(codes, [0])
        self.assertEqual(len(spawned), 1)
        argv = spawned[0].argv
        self.assertIn("--pool-workers", argv)
        self.assertEqual(argv[argv.index("--pool-workers") + 1], "3")
        self.assertEqual(argv[argv.index("--label") + 1], "pool")
        env = spawned[0].kwargs["env"]
        self.assertEqual(env["LINKER_POOL_RESTART_LIMIT"], "2")
        self.assertEqual(env["LINKER_POOL_STALL_SECONDS"], "1800.0")

    def test_pool_mode_clears_a_stale_master_heartbeat_before_spawning(self):
        # The driver's watchdog reads worker-heartbeats/pool: a file left by a
        # previous life must not make a fresh master look alive (or stale) early.
        run_dir = os.path.join(tempfile.mkdtemp(), "run")
        os.makedirs(os.path.join(run_dir, "worker-heartbeats"))
        stale = os.path.join(run_dir, "worker-heartbeats", "pool")
        open(stale, "w").close()
        self._run(run_dir=run_dir, engine_pool=True)
        self.assertFalse(os.path.exists(stale))

    def test_default_mode_still_spawns_independent_workers(self):
        codes, spawned = self._run()
        self.assertEqual(codes, [0, 0, 0])
        labels = sorted(p.argv[p.argv.index("--label") + 1] for p in spawned)
        self.assertEqual(labels, ["w01", "w02", "w03"])
        self.assertTrue(all("--pool-workers" not in p.argv for p in spawned))


if __name__ == "__main__":
    unittest.main()
