"""mongod must not outlive the job that started it.

Failed recovery attempt 34015274945 ended with the runner's generic reaper printing
`Terminate orphan process: pid (1413) (mongod)` in *Complete job* — that reaper runs
only on a clean exit, so a runner death or a hard cancel leaves the --fork daemon
behind.  ci/stop_mongod.sh is the owned stop path; these tests drive it against a stub
daemon in a sandbox and lock the two guards that keep it from stopping anything else.
"""
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
STOP = ROOT / "ci" / "stop_mongod.sh"
SCOPE_TOOL = ROOT / "ci" / "process_scope.py"


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@unittest.skipUnless(sys.platform.startswith("linux"), "process ownership is Linux-only")
class StopMongodTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name)
        (self.cache / "mongo-data").mkdir()
        self.scope = self.cache / "mongod.scope.json"
        self.owner = self.cache / "mongod.owner"
        self.pidfile = self.cache / "mongod.pid"
        self.daemon = None
        self.addCleanup(self._kill_daemon)
        self.addCleanup(self.temp.cleanup)

    def _kill_daemon(self):
        if self.daemon is not None and self.daemon.poll() is None:
            self.daemon.kill()
            self.daemon.wait(timeout=10)

    def _start_daemon(self):
        """A stand-in for `mongod --fork`: its own session leader, carrying the same
        --dbpath argument the ownership scope is bound to."""
        stub = self.cache / "stub_mongod.py"
        stub.write_text("import time\nwhile True:\n    time.sleep(0.2)\n", encoding="utf-8")
        self.daemon = subprocess.Popen(
            [sys.executable, str(stub), "--dbpath", f"{self.cache}/mongo-data", "--fork"],
            start_new_session=True,
        )
        self.pidfile.write_text(str(self.daemon.pid), encoding="ascii")
        return self.daemon.pid

    def _record(self, pid):
        subprocess.run(
            [
                sys.executable, str(SCOPE_TOOL), "record",
                "--state", str(self.scope), "--pid", str(pid),
                "--kind", "mongod", "--expect", f"--dbpath {self.cache}/mongo-data",
            ],
            check=True,
        )

    def _stop(self, owner="42:1"):
        env = dict(os.environ, LINKER_CACHE_DIR=str(self.cache), LINKER_MONGO_OWNER=owner)
        result = subprocess.run(
            ["bash", str(STOP)], env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stdout)
        return lines[0]

    def test_owned_daemon_is_stopped_and_named(self):
        pid = self._start_daemon()
        self._record(pid)
        self.owner.write_text("42:1", encoding="ascii")
        self.assertEqual(self._stop(), f"mongod: stopped pid {pid}")
        deadline = time.time() + 10
        while _alive(pid) and time.time() < deadline:
            time.sleep(0.05)
        self.daemon.wait(timeout=10)
        self.assertFalse(self.scope.exists())
        self.assertFalse(self.owner.exists())
        self.assertFalse(self.pidfile.exists())

    def test_no_recorded_daemon_says_so_instead_of_staying_silent(self):
        self.assertEqual(self._stop(), "mongod: not running")

    def test_another_runs_daemon_is_never_stopped(self):
        pid = self._start_daemon()
        self._record(pid)
        self.owner.write_text("99:1", encoding="ascii")
        self.assertEqual(
            self._stop(owner="42:1"),
            "mongod: owned by 99:1, not 42:1 — left running",
        )
        self.assertTrue(_alive(pid))
        self.assertTrue(self.scope.exists())

    def test_daemon_that_already_died_is_reported_as_not_running(self):
        pid = self._start_daemon()
        self._record(pid)
        self.owner.write_text("42:1", encoding="ascii")
        os.kill(pid, signal.SIGKILL)
        self.daemon.wait(timeout=10)
        self.assertEqual(self._stop(), "mongod: not running")
        self.assertFalse(self.scope.exists())


class MongodOwnershipContractTest(unittest.TestCase):
    """The wiring itself: a stop path on every exit, and a pid worth stopping."""

    def setUp(self):
        self.workflow = (ROOT / ".github" / "workflows" / "relink.yml").read_text(encoding="utf-8")
        self.setup = (ROOT / "ci" / "setup_stack.sh").read_text(encoding="utf-8")

    def test_setup_stack_records_an_identity_bound_owner_for_the_daemon(self):
        self.assertIn('--pidfilepath "$MONGO_PIDFILE"', self.setup)
        self.assertRegex(
            self.setup,
            r'record --state "\$MONGO_SCOPE" --pid "\$MONGO_PID"',
        )
        self.assertIn('--kind mongod --expect "--dbpath $CACHE/mongo-data"', self.setup)
        self.assertIn('> "$MONGO_OWNER_FILE"', self.setup)

    def test_every_exit_path_of_a_mongod_job_stops_it(self):
        # the compute step's EXIT trap, plus BOTH always() cleanup steps (compute and
        # resolver) — the paths a cancel or a runner death leaves behind.
        self.assertEqual(self.workflow.count("bash ci/stop_mongod.sh"), 3)
        trap = self.workflow.split("finish_compute() {", 1)[1].split("trap finish_compute EXIT", 1)[0]
        self.assertIn("bash ci/stop_mongod.sh", trap)
        always_cleanups = re.findall(
            r"if: always\(\)(?:(?!- name:).)*?bash ci/stop_mongod\.sh",
            self.workflow,
            re.S,
        )
        self.assertEqual(len(always_cleanups), 2)

    def test_the_stop_script_prints_one_line_and_never_fails_the_job(self):
        script = STOP.read_text(encoding="utf-8")
        self.assertIn('echo "mongod: not running"', script)
        self.assertIn('echo "mongod: stopped pid $PID"', script)
        for exit_code in re.findall(r"^exit (\d+)$", script, re.M):
            self.assertEqual(exit_code, "0", script)


if __name__ == "__main__":
    unittest.main()
