import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "ci" / "process_scope.py"


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@unittest.skipUnless(sys.platform.startswith("linux"), "/proc identity contract is Linux-only")
class ProcessScopeTest(unittest.TestCase):
    def test_terminate_kills_the_owned_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "scope.json"
            proc = subprocess.Popen(["bash", "-c", "trap '' TERM; sleep 300 & wait"], start_new_session=True)
            try:
                subprocess.run([
                    sys.executable, str(TOOL), "record", "--state", str(state),
                    "--pid", str(proc.pid), "--kind", "test", "--expect", "bash",
                ], check=True)
                subprocess.run([
                    sys.executable, str(TOOL), "terminate", "--state", str(state),
                    "--expect", "bash", "--grace", "0.2",
                ], check=True)
                proc.wait(timeout=5)
                self.assertFalse(alive(proc.pid))
                self.assertFalse(state.exists())
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)

    def test_start_time_mismatch_never_signals_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "scope.json"
            proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
            try:
                subprocess.run([
                    sys.executable, str(TOOL), "record", "--state", str(state),
                    "--pid", str(proc.pid), "--kind", "test", "--expect", "sleep",
                ], check=True)
                value = json.loads(state.read_text())
                value["identity"]["start_ticks"] += 1
                state.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
                result = subprocess.run([
                    sys.executable, str(TOOL), "terminate", "--state", str(state),
                    "--expect", "sleep", "--grace", "0.1",
                ], check=False)
                self.assertEqual(result.returncode, 2)
                self.assertTrue(alive(proc.pid), "mismatched identity killed an unrelated process")
                self.assertTrue(state.exists(), "live unverifiable scope was discarded")
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
