import importlib.util
from pathlib import Path
import sys
import threading
import time
import unittest


def _module():
    path = Path(__file__).parents[1] / "ci" / "gpu_server_microbatch.py"
    spec = importlib.util.spec_from_file_location("gpu_server_microbatch", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class OrderedMicroBatcherTest(unittest.TestCase):
    def test_concurrent_requests_share_one_ordered_model_call(self):
        module = _module()
        calls = []
        entered = threading.Event()

        def runner(texts, lang, with_span_text):
            calls.append((list(texts), lang, with_span_text))
            entered.set()
            return {"results": [{"text": text} for text in texts]}

        batcher = module.OrderedMicroBatcher(runner)
        original = batcher._max_texts
        batcher._max_texts = 4
        results = []

        def submit(texts):
            results.append(batcher.submit(texts, "he", False))

        first = threading.Thread(target=submit, args=(["a", "b", "c"],))
        second = threading.Thread(target=submit, args=(["d", "e", "f"],))
        first.start()
        second.start()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(entered.is_set())
        self.assertEqual(original, 100)
        self.assertEqual(sum(len(call[0]) for call in calls), 6)
        self.assertEqual(sorted(item["text"] for result in results for item in result["results"]), list("abcdef"))
        self.assertTrue(all(call[1:] == ("he", False) for call in calls))

    def test_incompatible_requests_do_not_share_a_model_call(self):
        module = _module()
        calls = []

        def runner(texts, lang, with_span_text):
            calls.append((list(texts), lang, with_span_text))
            return {"results": [{"text": text} for text in texts]}

        batcher = module.OrderedMicroBatcher(runner)
        self.assertEqual(batcher.submit(["a"], "he", False), {"results": [{"text": "a"}]})
        self.assertEqual(batcher.submit(["b"], "en", False), {"results": [{"text": "b"}]})
        self.assertEqual(calls, [(["a"], "he", False), (["b"], "en", False)])

    def test_empty_request_matches_upstream_shape(self):
        module = _module()
        batcher = module.OrderedMicroBatcher(lambda *_: self.fail("runner called"))
        self.assertEqual(batcher.submit([], "he", False), {"results": []})
