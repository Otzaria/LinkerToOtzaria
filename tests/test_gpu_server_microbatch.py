import importlib.util
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch


def _module():
    path = Path(__file__).parents[1] / "ci" / "gpu_server_microbatch.py"
    spec = importlib.util.spec_from_file_location("gpu_server_microbatch", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _batcher(module, runner, *, maximum=150, wait_ms=8, timeout=2):
    values = {
        "LINKER_NER_MICROBATCH_TEXTS": str(maximum),
        "LINKER_NER_MICROBATCH_WAIT_MS": str(wait_ms),
        "LINKER_NER_REQUEST_TIMEOUT_SECONDS": str(timeout),
    }
    with patch.dict(os.environ, values):
        return module.OrderedMicroBatcher(runner)


class OrderedMicroBatcherTest(unittest.TestCase):
    def test_concurrent_requests_share_one_ordered_model_without_reordering(self):
        module = _module()
        calls = []

        def runner(texts, lang, with_span_text):
            calls.append((list(texts), lang, with_span_text))
            return {"results": [{"text": text} for text in texts]}

        batcher = _batcher(module, runner, maximum=4, wait_ms=100)
        outputs = {}
        gate = threading.Barrier(2)

        def submit(name, texts):
            gate.wait(timeout=1)
            outputs[name] = batcher.submit(texts, "he", False)

        first = threading.Thread(target=submit, args=("first", ["a", "b", "c"]))
        second = threading.Thread(target=submit, args=("second", ["d", "e", "f"]))
        first.start()
        second.start()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(outputs["first"], {"results": [{"text": x} for x in "abc"]})
        self.assertEqual(outputs["second"], {"results": [{"text": x} for x in "def"]})
        self.assertEqual(sorted(len(call[0]) for call in calls), [2, 4])
        self.assertTrue(all(call[1:] == ("he", False) for call in calls))

    def test_full_queued_batch_skips_coalescing_delay(self):
        module = _module()
        first_entered = threading.Event()
        release_first = threading.Event()
        full_batch_entered = threading.Event()
        calls = []

        def runner(texts, *_):
            calls.append(list(texts))
            if texts == ["block-1", "block-2", "block-3", "block-4"]:
                first_entered.set()
                release_first.wait(timeout=1)
            else:
                full_batch_entered.set()
            return {"results": [{"text": text} for text in texts]}

        batcher = _batcher(module, runner, maximum=4, wait_ms=500)
        threads = [
            threading.Thread(
                target=batcher.submit,
                args=(["block-1", "block-2", "block-3", "block-4"], "he", False),
            )
        ]
        threads[0].start()
        self.assertTrue(first_entered.wait(timeout=1))
        for texts in (["a", "b"], ["c", "d"]):
            thread = threading.Thread(target=batcher.submit, args=(texts, "he", False))
            thread.start()
            threads.append(thread)
        time.sleep(0.03)
        started = time.monotonic()
        release_first.set()
        self.assertTrue(full_batch_entered.wait(timeout=0.2))
        self.assertLess(time.monotonic() - started, 0.2)
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(calls[1], ["a", "b", "c", "d"])

    def test_failed_partial_ticket_is_not_requeued(self):
        module = _module()
        calls = []

        def runner(texts, *_):
            calls.append(list(texts))
            if len(calls) == 2:
                raise ValueError("model failed")
            return {"results": [{"text": text} for text in texts]}

        batcher = _batcher(module, runner, maximum=4, wait_ms=0)
        with self.assertRaisesRegex(ValueError, "model failed"):
            batcher.submit(list("abcdef"), "he", False)
        time.sleep(0.05)
        self.assertEqual(calls, [list("abcd"), list("ef")])
        self.assertEqual(batcher.metrics()["failures"], 1)

    def test_timed_out_queued_ticket_is_removed(self):
        module = _module()
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def runner(texts, *_):
            calls.append(list(texts))
            entered.set()
            release.wait(timeout=1)
            return {"results": [{"text": text} for text in texts]}

        batcher = _batcher(module, runner, maximum=1, wait_ms=0, timeout=1)
        first = threading.Thread(target=batcher.submit, args=(["first"], "he", False))
        first.start()
        self.assertTrue(entered.wait(timeout=1))
        batcher._request_timeout_seconds = 0.05
        with self.assertRaisesRegex(TimeoutError, "exceeded"):
            batcher.submit(["second"], "he", False)
        release.set()
        first.join(timeout=2)
        self.assertFalse(first.is_alive())
        time.sleep(0.05)
        self.assertEqual(calls, [["first"]])
        self.assertEqual(batcher.metrics()["timeouts"], 1)

    def test_model_is_never_called_concurrently(self):
        module = _module()
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        def runner(texts, *_):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return {"results": [{"text": text} for text in texts]}

        batcher = _batcher(module, runner, maximum=3, wait_ms=20)
        threads = [
            threading.Thread(target=batcher.submit, args=([str(number)], "he", False))
            for number in range(12)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(maximum_active, 1)

    def test_incompatible_requests_do_not_share_a_model_call(self):
        module = _module()
        calls = []

        def runner(texts, lang, with_span_text):
            calls.append((list(texts), lang, with_span_text))
            return {"results": [{"text": text} for text in texts]}

        batcher = _batcher(module, runner)
        self.assertEqual(batcher.submit(["a"], "he", False), {"results": [{"text": "a"}]})
        self.assertEqual(batcher.submit(["b"], "en", False), {"results": [{"text": "b"}]})
        self.assertEqual(calls, [(["a"], "he", False), (["b"], "en", False)])

    def test_empty_request_matches_upstream_shape(self):
        module = _module()
        batcher = _batcher(module, lambda *_: self.fail("runner called"))
        self.assertEqual(batcher.submit([], "he", False), {"results": []})


if __name__ == "__main__":
    unittest.main()
