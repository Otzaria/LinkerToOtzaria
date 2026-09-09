"""What is alive when the resolver pool forks, and what the log says about it.

Every relink log of 2026-09-06 carries two warnings about that one instant —
CPython's ``This process (pid=N) is multi-threaded, use of fork() may lead to
deadlocks in the child.`` (raised at the ``os.fork()`` in ``run_pool``) and pymongo's
``MongoClient opened before fork`` — and neither names a thread, so a post-mortem
cannot tell whether the threads were the pipeline's to stop.  link_books now states
the inventory once per run and suppresses exactly the one message that inventory
answers; these tests pin both halves, including that nothing wider is suppressed.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

CAN_FORK = hasattr(os, "fork")
FORK_WARNING = "multi-threaded, use of fork()"


@unittest.skipUnless(CAN_FORK, "link_books imports the POSIX resource module")
class ForkThreadInventoryTest(unittest.TestCase):
    def setUp(self):
        import link_books
        self.link_books = link_books

    def test_inventory_names_every_live_python_thread(self):
        stop = threading.Event()
        thread = threading.Thread(target=stop.wait, name="probe-monitor", daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(stop.set)
        line = self.link_books.fork_thread_inventory()
        self.assertTrue(line.startswith("fork-time threads: "), line)
        self.assertIn("probe-monitor", line)
        self.assertIn(f"{len(threading.enumerate())} python", line)
        self.assertTrue(line.endswith(" OS"), line)

    def test_inventory_reports_the_kernel_count_or_says_it_cannot(self):
        line = self.link_books.fork_thread_inventory()
        count = line.rsplit(",", 1)[1].strip().split()[0]
        if sys.platform.startswith("linux"):
            self.assertGreaterEqual(int(count), 1)
            self.assertGreaterEqual(int(count), len(threading.enumerate()))
        else:
            self.assertIn(count, {"unknown", *[str(n) for n in range(1, 4096)]})


def _program(body, **values):
    return textwrap.dedent(body).format(src=repr(SRC), **values)


@unittest.skipUnless(CAN_FORK, "run_pool forks; POSIX only")
class ForkWarningTest(unittest.TestCase):
    """The filter must be narrow: this environment has to warn without it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _run(self, program):
        result = subprocess.run(
            [sys.executable, "-W", "always::DeprecationWarning", "-c", program],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_a_bare_fork_with_a_live_thread_still_warns(self):
        result = self._run(_program("""\
            import os, sys, threading
            sys.path.insert(0, {src})
            stop = threading.Event()
            threading.Thread(target=stop.wait, name="probe", daemon=True).start()
            pid = os.fork()
            if pid == 0:
                os._exit(0)
            os.waitpid(pid, 0)
            stop.set()
        """))
        if FORK_WARNING not in result.stderr:
            self.skipTest("this interpreter does not warn about fork() with threads")

    def test_run_pool_suppresses_only_that_message_and_names_the_threads(self):
        run_dir = os.path.join(self.tmp.name, "run")
        os.makedirs(run_dir)
        result = self._run(_program("""\
            import os, sys, threading, warnings
            sys.path.insert(0, {src})
            import link_books
            stop = threading.Event()
            threading.Thread(target=stop.wait, name="probe-monitor", daemon=True).start()
            lines = []
            code = link_books.run_pool(
                1, {run_dir}, "master",
                lambda label: None,
                lambda: 0,
                log=lines.append,
                poll_seconds=0.05,
            )
            stop.set()
            warnings.warn("an unrelated deprecation", DeprecationWarning)
            print("EXIT", code)
            for line in lines:
                print("LOG", line)
        """, run_dir=repr(run_dir)))
        self.assertNotIn(FORK_WARNING, result.stderr)
        # the filter is scoped to one message, not to DeprecationWarning at large
        self.assertIn("an unrelated deprecation", result.stderr)
        self.assertIn("EXIT 0", result.stdout)
        inventory = [
            line for line in result.stdout.splitlines()
            if line.startswith("LOG pool master: fork-time threads: ")
        ]
        self.assertEqual(len(inventory), 1, result.stdout)
        self.assertIn("probe-monitor", inventory[0])


if __name__ == "__main__":
    unittest.main()
