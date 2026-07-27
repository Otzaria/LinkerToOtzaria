import os
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
