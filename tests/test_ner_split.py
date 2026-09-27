import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

import precompute_ner  # noqa: E402
from ci import cleanup_local_ner_cache  # noqa: E402
from ner_handoff import NerBundle, SCHEMA_VERSION  # noqa: E402


class Response:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text
        self.ok = 200 <= status < 300


class NerTransportTest(unittest.TestCase):
    @staticmethod
    def requests_module(responses):
        return SimpleNamespace(
            post=mock.Mock(side_effect=responses),
            ConnectionError=type("ConnectionError", (Exception,), {}),
            Timeout=type("Timeout", (Exception,), {}),
        )

    def test_transient_gateway_response_is_retried_with_same_batch(self):
        responses = [
            Response(503, "temporary"),
            Response(200, json.dumps({"results": [{"entities": []}]})),
        ]
        requests = self.requests_module(responses)
        with mock.patch.dict(sys.modules, {"requests": requests}), \
                mock.patch.object(precompute_ner.time, "sleep"):
            self.assertEqual(
                precompute_ner._post_bulk("http://gpu", ["אב"]),
                [{"entities": []}],
            )
        post = requests.post
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["json"], post.call_args_list[1].kwargs["json"])

    def test_nontransient_http_error_preserves_status_and_body(self):
        requests = self.requests_module([Response(500, "model exploded")])
        with mock.patch.dict(sys.modules, {"requests": requests}):
            with self.assertRaisesRegex(RuntimeError, "HTTP 500.*model exploded"):
                precompute_ner._post_bulk("http://gpu", ["אב"])

    def test_invalid_json_preserves_bounded_response_diagnostics(self):
        requests = self.requests_module([Response(200, "not-json")])
        with mock.patch.dict(sys.modules, {"requests": requests}):
            with self.assertRaisesRegex(RuntimeError, "invalid.*status=200.*not-json"):
                precompute_ner._post_bulk("http://gpu", ["אב"])


class NerBundleBoundaryTest(unittest.TestCase):
    def test_manifest_binds_character_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ner_manifest.json").write_text(
                json.dumps({
                    "schema_version": SCHEMA_VERSION,
                    "relink_request_id": "a" * 64,
                    "snapshot_sha256": "b" * 64,
                    "engine_fingerprint": "engine",
                    "batch_lines": 100,
                    "batch_chars": 120000,
                    "books": [],
                }),
                encoding="utf-8",
            )
            NerBundle(
                root,
                request_id="a" * 64,
                snapshot_sha256="b" * 64,
                engine_fingerprint="engine",
                changed_books=[],
                expected_batch_lines=100,
                expected_batch_chars=120000,
            )
            with self.assertRaisesRegex(RuntimeError, "character budget"):
                NerBundle(
                    root,
                    request_id="a" * 64,
                    snapshot_sha256="b" * 64,
                    engine_fingerprint="engine",
                    changed_books=[],
                    expected_batch_chars=60000,
                )


class LocalNerCacheCleanupTest(unittest.TestCase):
    def test_cleanup_removes_only_the_exact_completed_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            request = "a" * 64
            sibling = "b" * 64
            (root / "raw-ner" / request).mkdir(parents=True)
            (root / "raw-ner" / sibling).mkdir()
            with mock.patch.object(
                sys,
                "argv",
                [
                    "cleanup_local_ner_cache.py",
                    "--cache-root",
                    str(root),
                    "--request-id",
                    request,
                ],
            ):
                cleanup_local_ner_cache.main()
            self.assertFalse((root / "raw-ner" / request).exists())
            self.assertTrue((root / "raw-ner" / sibling).is_dir())

    @staticmethod
    def run_cleanup(root, request, *extra):
        argv = ["cleanup_local_ner_cache.py", "--cache-root", str(root), "--request-id", request, *extra]
        with mock.patch.object(sys, "argv", argv):
            cleanup_local_ner_cache.main()

    @staticmethod
    def age(path, days):
        stamp = time.time() - days * 86400
        for dirpath, dirnames, filenames in os.walk(path, topdown=False):
            for name in filenames + dirnames:
                os.utime(os.path.join(dirpath, name), (stamp, stamp), follow_symlinks=False)
        os.utime(path, (stamp, stamp), follow_symlinks=False)

    def test_batch_checkpoint_survives_until_the_payload_shipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            request, sibling = "a" * 64, "b" * 64
            for name in (request, sibling):
                (root / name / "source-1-1").mkdir(parents=True)
                (root / f".save-{name}.lock").touch()
            self.run_cleanup(root, request)
            self.assertTrue((root / request / "source-1-1").is_dir())
            self.run_cleanup(root, request, "--drop-batch-checkpoint")
            self.assertFalse((root / request).exists())
            self.assertFalse((root / f".save-{request}.lock").exists())
            self.assertTrue((root / sibling / "source-1-1").is_dir())
            self.assertTrue((root / f".save-{sibling}.lock").exists())

    def test_sweep_removes_only_other_requests_idle_past_the_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            current, stale, fresh, orphan, linked = (c * 64 for c in "abcde")
            for parent in (root, root / "raw-ner"):
                for name in (current, stale, fresh):
                    (parent / name / "source-1-1").mkdir(parents=True)
                    (parent / name / "source-1-1" / "manifest.json").write_text("{}")
            for name in (current, stale, fresh, orphan):
                (root / f".save-{name}.lock").touch()
            (root / "notes").mkdir()
            outside = root / "outside"
            (outside / "keep").mkdir(parents=True)
            (root / linked).symlink_to(outside, target_is_directory=True)
            for entry in [*root.iterdir(), *(root / "raw-ner").iterdir()]:
                if entry.name != "raw-ner":
                    self.age(entry, 30)
            # A fresh file deep inside keeps an otherwise old checkpoint.
            (root / fresh / "source-1-1" / "manifest.json").touch()
            (root / "raw-ner" / fresh / "source-1-1" / "manifest.json").touch()

            self.run_cleanup(root, current, "--max-age-days", "14")

            for parent in (root, root / "raw-ner"):
                self.assertFalse((parent / stale).exists())
                self.assertTrue((parent / fresh).is_dir())
            self.assertFalse((root / "raw-ner" / current).exists())
            self.assertTrue((root / current).is_dir())
            self.assertTrue((root / f".save-{current}.lock").exists())
            self.assertTrue((root / f".save-{fresh}.lock").exists())
            self.assertFalse((root / f".save-{stale}.lock").exists())
            self.assertFalse((root / f".save-{orphan}.lock").exists())
            self.assertTrue((root / "notes").is_dir())
            self.assertTrue((root / linked).is_symlink())
            self.assertTrue((outside / "keep").is_dir())


if __name__ == "__main__":
    unittest.main()
