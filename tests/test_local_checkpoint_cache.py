import argparse
import json
from pathlib import Path
import tempfile
import unittest

from ci import local_checkpoint_cache as cache


class LocalCheckpointCacheTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        self.cache = self.root / "cache"
        claim = "a" * 40
        (self.run / "checkpoints" / claim).mkdir(parents=True)
        (self.run / "changed_books.json").write_text('[{"book":"exact"}]', encoding="utf-8")
        (self.run / "checkpoints" / claim / "000000000000.jsonl").write_text(
            '{"record":"exact"}\n', encoding="utf-8"
        )
        self.args = argparse.Namespace(
            cache_root=str(self.cache),
            run_dir=str(self.run),
            source_run_id=123,
            source_run_attempt=1,
            request_id="b" * 64,
            parent_run_id=456,
            parent_run_attempt=2,
            snapshot_sha256="c" * 64,
            sefaria_tag="tag-1",
            sefaria_metadata_sha256="d" * 64,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_restores_only_plan_and_immutable_shards(self):
        cache.save(self.args)
        (self.run / "changed_books.json").unlink()
        (self.run / "checkpoints" / ("a" * 40) / "000000000000.jsonl").unlink()
        (self.run / "done").mkdir()
        (self.run / "done" / "stale").write_text("", encoding="utf-8")

        cache.restore(self.args)

        self.assertEqual(
            json.loads((self.run / "changed_books.json").read_text(encoding="utf-8")),
            [{"book": "exact"}],
        )
        self.assertTrue(
            (self.run / "checkpoints" / ("a" * 40) / "000000000000.jsonl").is_file()
        )
        self.assertTrue((self.run / "done" / "stale").is_file())

    def test_digest_tamper_is_rejected(self):
        cache.save(self.args)
        source = cache.cache_path(self.args)
        shard = next((source / "checkpoints").rglob("*.jsonl"))
        shard.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "file set/size/digest mismatch"):
            cache.restore(self.args)

    def test_identity_mismatch_is_rejected(self):
        cache.save(self.args)
        self.args.snapshot_sha256 = "e" * 64
        with self.assertRaisesRegex(RuntimeError, "snapshot_sha256 mismatch"):
            cache.restore(self.args)

    def test_unexpected_member_is_rejected(self):
        cache.save(self.args)
        source = cache.cache_path(self.args)
        (source / "claim").mkdir()
        (source / "claim" / "unsafe").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "unsafe/unexpected checkpoint member"):
            cache.restore(self.args)


if __name__ == "__main__":
    unittest.main()
