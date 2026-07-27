import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import link_books  # noqa: E402


class ClaimFreshnessTests(unittest.TestCase):
    def test_new_claim_without_heartbeat_is_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            claim = os.path.join(tmp, "claim")
            heartbeat = os.path.join(claim, "hb")
            os.mkdir(claim)
            now = os.path.getmtime(claim) + 1

            self.assertTrue(link_books.claim_is_fresh(claim, heartbeat, now=now))

    def test_old_claim_without_heartbeat_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            claim = os.path.join(tmp, "claim")
            heartbeat = os.path.join(claim, "hb")
            os.mkdir(claim)
            old = time.time() - link_books.CLAIM_STALE_SEC - 1
            os.utime(claim, (old, old))

            self.assertFalse(link_books.claim_is_fresh(claim, heartbeat))

    def test_only_one_worker_atomically_takes_over_stale_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            for directory in ("claim", "done"):
                os.mkdir(os.path.join(tmp, directory))
            cid = "a" * 40
            claim = os.path.join(tmp, "claim", cid)
            heartbeat = os.path.join(claim, "hb")
            os.mkdir(claim)
            Path(heartbeat).touch()
            old = time.time() - link_books.CLAIM_STALE_SEC - 1
            os.utime(heartbeat, (old, old))
            barrier = threading.Barrier(12)
            results = []

            def attempt():
                barrier.wait()
                results.append(link_books.try_claim_atomic(tmp, cid))

            workers = [threading.Thread(target=attempt) for _ in range(12)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            self.assertEqual(results.count(True), 1)
            self.assertEqual(results.count(False), 11)


if __name__ == "__main__":
    unittest.main()
