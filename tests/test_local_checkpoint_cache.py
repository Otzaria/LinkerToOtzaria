import argparse
import hashlib
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
        self.repo = self.root / "repo"
        self.book = {
            "source_name": "Source",
            "canonical_he_title": "Book",
            "hash": "1" * 16,
            "ner_ranges": [[0, 1]],
            "reuse": [],
        }
        claim = hashlib.sha1(b"Source\0Book").hexdigest()
        self.claim = claim
        (self.run / "checkpoints" / claim).mkdir(parents=True)
        (self.run / "changed_books.json").write_text(
            json.dumps([self.book]), encoding="utf-8"
        )
        (self.run / "checkpoints" / claim / "000000000000.jsonl").write_text(
            '{"record":"exact"}\n', encoding="utf-8"
        )
        (self.run / "done").mkdir()
        (self.run / "done" / claim).write_text("", encoding="utf-8")
        artifact = self.repo / "artifacts" / "Source" / "Book.jsonl"
        artifact.parent.mkdir(parents=True)
        artifact.write_text('{"record":"complete"}\n', encoding="utf-8")
        self.args = argparse.Namespace(
            cache_root=str(self.cache),
            run_dir=str(self.run),
            repo=str(self.repo),
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

    def test_round_trip_restores_plan_shards_and_completed_outputs(self):
        prior = self.run / "checkpoints" / self.claim / "prior.jsonl"
        prior.write_text('{"record":"prior"}\n', encoding="utf-8")
        locks = self.run / "checkpoints" / self.claim / ".batch-locks"
        locks.mkdir()
        (locks / "000000000000.lock").write_text("ephemeral", encoding="utf-8")
        cache.save(self.args)
        (self.run / "changed_books.json").unlink()
        (self.run / "checkpoints" / self.claim / "000000000000.jsonl").unlink()
        (self.run / "done" / "stale").write_text("", encoding="utf-8")

        cache.restore(self.args)

        self.assertEqual(
            json.loads((self.run / "changed_books.json").read_text(encoding="utf-8")),
            [self.book],
        )
        self.assertTrue(
            (self.run / "checkpoints" / self.claim / "000000000000.jsonl").is_file()
        )
        self.assertEqual(
            (self.run / "checkpoints" / self.claim / "prior.jsonl").read_text(
                encoding="utf-8"
            ),
            '{"record":"prior"}\n',
        )
        self.assertFalse(
            (self.run / "checkpoints" / self.claim / ".batch-locks").exists()
        )
        completed = json.loads((self.run / "completed_books.json").read_text(encoding="utf-8"))
        self.assertEqual(completed[0]["claim_id"], self.claim)
        self.assertEqual(completed[0]["artifact"], f"{self.claim}.jsonl")
        self.assertTrue((self.run / "completed_artifacts" / f"{self.claim}.jsonl").is_file())
        self.assertTrue((self.run / "done" / "stale").is_file())

    def test_schema_one_cache_remains_restorable(self):
        cache.save(self.args)
        source = cache.cache_path(self.args)
        (source / "completed_books.json").unlink()
        if (source / "completed_artifacts").is_dir():
            for item in (source / "completed_artifacts").iterdir():
                item.unlink()
            (source / "completed_artifacts").rmdir()
        manifest_path = source / "checkpoint_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = 1
        manifest["files"] = cache.listed_files(source)
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        cache.restore(self.args)
        self.assertFalse((self.run / "completed_books.json").exists())

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
