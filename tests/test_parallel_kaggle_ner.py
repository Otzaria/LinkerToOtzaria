import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "parallel_kaggle_ner",
    ROOT / "scripts" / "parallel_kaggle_ner.py",
)
parallel_kaggle_ner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parallel_kaggle_ner)


class ParallelKaggleNerTest(unittest.TestCase):
    def test_status_classification_distinguishes_missing_from_failed(self):
        classify = parallel_kaggle_ner.state_of
        self.assertEqual(classify('status "KernelWorkerStatus.COMPLETE"'), "complete")
        self.assertEqual(classify('status "KernelWorkerStatus.RUNNING"'), "active")
        self.assertEqual(classify("404 Client Error: Not Found"), "pending")
        self.assertEqual(
            classify("Permission 'kernels.get' was denied"),
            "pending",
        )
        self.assertEqual(classify('status "KernelWorkerStatus.ERROR"'), "failed")

    def test_prepared_kernel_is_a_self_contained_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "worker.py"
            worker.write_text(
                "import sys\n\n"
                "def main():\n"
                "    print(sys.argv)\n\n"
                'if __name__ == "__main__":\n'
                "    main()\n",
                encoding="utf-8",
            )
            kernel = parallel_kaggle_ner.prepare_kernel(
                root / "state",
                worker,
                prefix="test-ner",
                dataset="owner/input",
                runtime_kernel="owner/runtime",
                index=7,
                session_budget_seconds=123,
            )
            source = (kernel / "run.py").read_text(encoding="utf-8")
            self.assertIn("def main():", source)
            self.assertIn('"--shard-index", \'7\'', source)
            self.assertIn('"--session-budget-seconds", \'123\'', source)
            self.assertFalse((kernel / "parallel_ner_worker.py").exists())


if __name__ == "__main__":
    unittest.main()
