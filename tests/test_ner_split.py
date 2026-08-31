import json
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
