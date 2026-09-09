"""Process-ownership acceptance tests for incremental._run_engine.

The seventh review's demand, verified for real: kill the driver (SIGTERM, as a job
cancel does) and assert that no engine worker — and no grandchild a worker spawned —
survives, because every worker runs in its own session/process group and the driver's
terminate handler turns the signal into an orderly group TERM → wait → KILL.
Also locks the defence-in-depth contract: when fd 9 (the host lease) is open in the
driver, workers inherit it (pass_fds), so an orphaned worker keeps the lease held
until the relink-start reaper clears it.
"""
import os
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")


def _driver_code(tmp, fake_python, open_lease_fd=False):
    return textwrap.dedent(f"""\
        import os, sys, types
        sys.path.insert(0, {SRC!r})
        import incremental
        incremental.install_terminate_handler()
        if {open_lease_fd!r}:
            fd = os.open({os.path.join(tmp, 'lease.lock')!r}, os.O_CREAT | os.O_RDWR)
            os.dup2(fd, 9)
        args = types.SimpleNamespace(
            python={fake_python!r}, snapshot="s", repo="r", run_dir="d",
            sef_project={tmp!r}, bavli_convention=False, engine_workers=2)
        incremental._run_engine(args, "only.txt")
    """)


def _pool_driver_code(tmp, fake_python, only_books, run_dir):
    """A real outer driver for the leader-dies/children-live regression fixture."""
    return textwrap.dedent(f"""\
        import sys, types
        sys.path.insert(0, {SRC!r})
        import incremental
        args = types.SimpleNamespace(
            python={fake_python!r}, snapshot="s", repo="r", run_dir={run_dir!r},
            sef_project={tmp!r}, bavli_convention=False, engine_workers=1,
            engine_pool=True, engine_restart_limit=1, worker_stall_seconds=60)
        incremental._run_engine(args, {only_books!r})
    """)


def _read_pids(path):
    if not os.path.exists(path):
        return []
    return [int(line) for line in Path(path).read_text().split() if line.strip()]


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _running(pid):
    """Unlike kill(pid, 0), do not call an already-exited unreaped zombie alive."""
    try:
        tail = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return tail[0] != "Z"
    except FileNotFoundError:
        return False


class ProcessHygieneTest(unittest.TestCase):
    def test_nonzero_worker_is_replaced_and_completes_exact_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = os.path.join(tmp, "run")
            os.makedirs(os.path.join(run_dir, "done"))
            only = os.path.join(tmp, "only.json")
            with open(only, "w", encoding="utf-8") as fh:
                json.dump([{"source_name": "Source", "canonical_he_title": "Book"}], fh)

            sys.path.insert(0, SRC)
            import incremental
            from linker_artifact import BookKey
            from link_books import claim_id
            cid = claim_id(BookKey("Source", "Book"))
            fake = os.path.join(tmp, "fake_python")
            with open(fake, "w") as fh:
                fh.write(textwrap.dedent(f"""\
                    #!/usr/bin/env bash
                    if [ ! -e "{tmp}/first-exit" ]; then
                      touch "{tmp}/first-exit"
                      exit 7
                    fi
                    touch "{run_dir}/done/{cid}"
                    exit 0
                """))
            os.chmod(fake, 0o755)
            import types
            args = types.SimpleNamespace(
                python=fake, snapshot="s", repo="r", run_dir=run_dir,
                sef_project=tmp, bavli_convention=False, engine_workers=1,
                engine_restart_limit=2,
            )

            self.assertEqual(incremental._run_engine(args, only), [7, 0])
            self.assertTrue(os.path.isfile(os.path.join(run_dir, "done", cid)))

    def test_sigterm_driver_leaves_no_worker_or_grandchild(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "fake_python")
            with open(fake, "w") as fh:
                fh.write(textwrap.dedent(f"""\
                    #!/usr/bin/env bash
                    echo $$ >> "{tmp}/workers.pids"
                    ( /bin/sleep 300 & echo $! >> "{tmp}/grandchildren.pids"; wait ) &
                    /bin/sleep 300
                """))
            os.chmod(fake, 0o755)
            driver = subprocess.Popen([sys.executable, "-c", _driver_code(tmp, fake)])
            try:
                deadline = time.time() + 15
                while time.time() < deadline and len(_read_pids(os.path.join(tmp, "grandchildren.pids"))) < 2:
                    time.sleep(0.2)
                workers = _read_pids(os.path.join(tmp, "workers.pids"))
                grandchildren = _read_pids(os.path.join(tmp, "grandchildren.pids"))
                self.assertEqual(len(workers), 2, "fixture never spawned workers")
                self.assertEqual(len(grandchildren), 2, "fixture never spawned grandchildren")

                driver.send_signal(signal.SIGTERM)
                driver.wait(timeout=60)

                deadline = time.time() + 10
                while time.time() < deadline and any(_alive(p) for p in workers + grandchildren):
                    time.sleep(0.2)
                leftovers = [p for p in workers + grandchildren if _alive(p)]
                for pid in leftovers:  # never leak even when failing the assertion
                    os.kill(pid, signal.SIGKILL)
                self.assertEqual(leftovers, [], "orphaned engine processes survived the driver")
            finally:
                if driver.poll() is None:
                    driver.kill()

    @unittest.skipUnless(sys.platform.startswith("linux"), "token/session ownership is Linux /proc based")
    def test_killed_pool_master_drains_old_children_before_replacement(self):
        """A dead master is not evidence that its old forked generation is gone.

        The first pool-master fixture forks a deliberately long-lived worker, then
        SIGKILLs itself.  The replacement writes an overlap marker if that worker is
        still running.  This is intentionally subprocess-level: a mocked Popen cannot
        model the leaderless PGID that caused the production double generation.
        """
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = os.path.join(tmp, "run")
            os.makedirs(os.path.join(run_dir, "done"))
            only = os.path.join(tmp, "only.json")
            with open(only, "w", encoding="utf-8") as stream:
                json.dump([{"source_name": "Source", "canonical_he_title": "Book"}], stream)
            fake = os.path.join(tmp, "fake_pool_master.py")
            with open(fake, "w", encoding="utf-8") as stream:
                stream.write(textwrap.dedent(f"""\
                    #!{sys.executable}
                    import os
                    from pathlib import Path
                    import signal
                    import subprocess
                    import sys
                    import time

                    root = Path({tmp!r})
                    first = root / "first-master"
                    worker_pid = root / "old-worker.pid"
                    worker_ready = root / "old-worker.ready"
                    terminated = root / "old-worker.terminated"
                    replacement = root / "replacement.started"
                    overlap = root / "overlap"
                    tokens = root / "session-tokens"

                    def running(pid):
                        try:
                            tail = Path(f"/proc/{{pid}}/stat").read_text().rsplit(")", 1)[1].split()
                            return tail[0] != "Z"
                        except FileNotFoundError:
                            return False

                    worker_code = (
                        "import os, signal, sys, time\\n"
                        "from pathlib import Path\\n"
                        "ready, terminated = map(Path, sys.argv[1:])\\n"
                        "ready.write_text(os.environ['LINKER_ENGINE_SESSION_TOKEN'] + '\\n' + str(time.monotonic()))\\n"
                        "def stop(*_):\\n"
                        "    terminated.write_text(str(time.monotonic()))\\n"
                        "    raise SystemExit(0)\\n"
                        "signal.signal(signal.SIGTERM, stop)\\n"
                        "while True:\\n"
                        "    time.sleep(0.05)\\n"
                    )
                    if not first.exists():
                        first.touch()
                        tokens.write_text(os.environ["LINKER_ENGINE_SESSION_TOKEN"] + "\\n")
                        child = subprocess.Popen([sys.executable, "-c", worker_code,
                                                  str(worker_ready), str(terminated)])
                        worker_pid.write_text(str(child.pid))
                        deadline = time.monotonic() + 5
                        while not worker_ready.exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        if not worker_ready.exists():
                            raise SystemExit("fixture worker did not become ready")
                        os.kill(os.getpid(), signal.SIGKILL)

                    old_pid = int(worker_pid.read_text())
                    if running(old_pid):
                        overlap.write_text("replacement started beside old worker")
                    with tokens.open("a") as stream:
                        stream.write(os.environ["LINKER_ENGINE_SESSION_TOKEN"] + "\\n")
                    replacement.write_text(str(time.monotonic()))
                """))
            os.chmod(fake, 0o755)
            driver = subprocess.Popen(
                [sys.executable, "-c", _pool_driver_code(tmp, fake, only, run_dir)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=dict(os.environ, LINKER_PROCESS_TERM_GRACE="1"),
            )
            old_pid = None
            try:
                stdout, stderr = driver.communicate(timeout=30)
                self.assertEqual(driver.returncode, 0, stdout + stderr)
                old_pid = int(Path(tmp, "old-worker.pid").read_text())
                self.assertTrue(Path(tmp, "old-worker.terminated").exists())
                self.assertTrue(Path(tmp, "replacement.started").exists())
                self.assertFalse(Path(tmp, "overlap").exists(), stdout + stderr)
                tokens = Path(tmp, "session-tokens").read_text().splitlines()
                self.assertEqual(len(tokens), 2)
                self.assertNotEqual(tokens[0], tokens[1])
                self.assertTrue(all(len(token) == 64 for token in tokens))
                self.assertEqual(Path(tmp, "old-worker.ready").read_text().splitlines()[0], tokens[0])
                self.assertLess(
                    float(Path(tmp, "old-worker.terminated").read_text()),
                    float(Path(tmp, "replacement.started").read_text()),
                )
                self.assertFalse(_running(old_pid), "old pool child survived the replacement")
            finally:
                if driver.poll() is None:
                    driver.kill()
                if old_pid is None and os.path.exists(os.path.join(tmp, "old-worker.pid")):
                    old_pid = int(Path(tmp, "old-worker.pid").read_text())
                if old_pid is not None and _running(old_pid):
                    os.kill(old_pid, signal.SIGKILL)

    def test_workers_inherit_open_lease_fd(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "fake_python")
            with open(fake, "w") as fh:
                fh.write(textwrap.dedent(f"""\
                    #!/usr/bin/env bash
                    if {{ exec 8>&9; }} 2>/dev/null; then
                      echo yes >> "{tmp}/fd9.seen"
                    else
                      echo no >> "{tmp}/fd9.seen"
                    fi
                    exit 0
                """))
            os.chmod(fake, 0o755)
            subprocess.run([sys.executable, "-c", _driver_code(tmp, fake, open_lease_fd=True)],
                           check=True, timeout=60)
            with open(os.path.join(tmp, "fd9.seen")) as fh:
                seen = fh.read().split()
            self.assertTrue(seen and all(v == "yes" for v in seen),
                            f"workers did not inherit fd 9: {seen}")

    def test_partial_spawn_failure_reaps_already_started_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "fake_python")
            with open(fake, "w") as fh:
                fh.write(textwrap.dedent(f"""\
                    #!/usr/bin/env bash
                    echo $$ >> "{tmp}/workers.pids"
                    exec /bin/sleep 300
                """))
            os.chmod(fake, 0o755)
            sys.path.insert(0, SRC)
            import incremental
            import types
            args = types.SimpleNamespace(
                python=fake, snapshot="s", repo="r", run_dir="d",
                sef_project=tmp, bavli_convention=False, engine_workers=2)
            real_popen = subprocess.Popen
            calls = 0
            spawned = []

            def fail_second(*pargs, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("adversarial second-spawn failure")
                proc = real_popen(*pargs, **kwargs)
                spawned.append(proc)
                return proc

            with mock.patch.object(subprocess, "Popen", side_effect=fail_second):
                with self.assertRaises(OSError):
                    incremental._run_engine(args, "only.txt")
            self.assertEqual(len(spawned), 1)
            workers = [spawned[0].pid]
            deadline = time.time() + 5
            while time.time() < deadline and _alive(workers[0]):
                time.sleep(0.1)
            if _alive(workers[0]):
                os.kill(workers[0], signal.SIGKILL)
            self.assertFalse(_alive(workers[0]), "worker from partial spawn survived")

    def test_second_term_does_not_interrupt_group_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "fake_python")
            with open(fake, "w") as fh:
                fh.write(textwrap.dedent(f"""\
                    #!/usr/bin/env bash
                    trap '' TERM INT
                    echo $$ >> "{tmp}/workers.pids"
                    while :; do sleep 1; done
                """))
            os.chmod(fake, 0o755)
            env = dict(os.environ, LINKER_PROCESS_TERM_GRACE="1")
            driver = subprocess.Popen([sys.executable, "-c", _driver_code(tmp, fake)], env=env)
            try:
                deadline = time.time() + 10
                while time.time() < deadline and len(_read_pids(os.path.join(tmp, "workers.pids"))) < 2:
                    time.sleep(0.1)
                workers = _read_pids(os.path.join(tmp, "workers.pids"))
                self.assertEqual(len(workers), 2)
                driver.send_signal(signal.SIGTERM)
                time.sleep(0.2)
                driver.send_signal(signal.SIGTERM)
                driver.wait(timeout=15)
                deadline = time.time() + 5
                while time.time() < deadline and any(_alive(pid) for pid in workers):
                    time.sleep(0.1)
                leftovers = [pid for pid in workers if _alive(pid)]
                for pid in leftovers:
                    os.kill(pid, signal.SIGKILL)
                self.assertEqual(leftovers, [], "second TERM interrupted process-group cleanup")
            finally:
                if driver.poll() is None:
                    driver.kill()


if __name__ == "__main__":
    unittest.main()
