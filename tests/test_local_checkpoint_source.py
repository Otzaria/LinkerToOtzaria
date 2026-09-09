import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "ci" / "validate_local_checkpoint_source.py"
SPEC = importlib.util.spec_from_file_location("validate_local_checkpoint_source", SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(guard)

REQUEST = "1f" * 32
HEAD_SHA = "d8" * 20


class LocalCheckpointSourceTest(unittest.TestCase):
    def args(self, **overrides):
        values = dict(
            run_id="33994031370", run_attempt=1, request_id=REQUEST,
            parent_run_id="33991433362", parent_run_attempt="1", head_sha=HEAD_SHA,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def source(self, **overrides):
        value = {
            "status": "completed",
            "conclusion": "cancelled",
            "event": "workflow_dispatch",
            "path": ".github/workflows/relink.yml",
            "run_attempt": 1,
            "display_title": f"relink request={REQUEST} parent=33991433362:1",
            "head_sha": HEAD_SHA,
        }
        value.update(overrides)
        return value

    def run_main(self, source, **overrides):
        args = self.args(**overrides)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            argv = [
                str(path), "--run-id", args.run_id,
                "--run-attempt", str(args.run_attempt), "--request-id", args.request_id,
                "--parent-run-id", args.parent_run_id,
                "--parent-run-attempt", args.parent_run_attempt,
                "--head-sha", args.head_sha,
            ]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = guard.main(argv)
            return code, stdout.getvalue()

    def test_first_recovery_accepts_the_original_relink_run(self):
        # The run a cycle's FIRST recovery chains from is always a plain
        # "relink request=…" attempt — the case the guard exists for.
        self.assertEqual(guard.rejections(self.source(), self.args()), [])
        self.assertEqual(self.run_main(self.source()), (0, ""))

    def test_later_recovery_accepts_a_recovery_run(self):
        title = f"relink-recovery request={REQUEST} parent=33991433362:1"
        source = self.source(display_title=title, conclusion="failure")
        self.assertEqual(guard.rejections(source, self.args()), [])

    def test_foreign_linker_commit_is_rejected_by_name(self):
        source = self.source(head_sha="ab" * 20)
        reasons = guard.rejections(source, self.args())
        self.assertEqual(len(reasons), 1)
        self.assertIn("head_sha 'abababab", reasons[0])
        self.assertIn("is not this Linker commit", reasons[0])

    def test_foreign_request_and_parent_are_rejected_by_name(self):
        for title in (
            f"relink request={'ee' * 32} parent=33991433362:1",
            f"relink-recovery request={REQUEST} parent=33991433362:2",
            f"relink-recovery request={REQUEST} parent=standalone",
            # The title match is EXACT equality, NEVER a prefix test. A prefix test
            # would accept a different parent attempt (":11" extends ":1") or any run
            # whose name merely starts with the expected coordinates.
            f"relink request={REQUEST} parent=33991433362:11",
            f"relink-recovery request={REQUEST} parent=33991433362:1 (rerun)",
        ):
            reasons = guard.rejections(self.source(display_title=title), self.args())
            self.assertEqual(len(reasons), 1)
            self.assertIn("display_title", reasons[0])
            self.assertIn("is neither", reasons[0])

    def test_non_terminal_source_is_rejected_by_name(self):
        for conclusion, status in (("success", "completed"), (None, "in_progress")):
            reasons = guard.rejections(
                self.source(conclusion=conclusion, status=status), self.args()
            )
            self.assertTrue(any("is not terminal" in reason for reason in reasons))

    def test_wrong_workflow_attempt_or_event_are_rejected_by_name(self):
        cases = {
            "run_attempt": {"run_attempt": 2},
            "path": {"path": ".github/workflows/kaggle-relink.yml"},
            "event": {"event": "schedule"},
        }
        for field, override in cases.items():
            reasons = guard.rejections(self.source(**override), self.args())
            self.assertEqual(len(reasons), 1)
            self.assertIn(field, reasons[0])
        # A JSON boolean must never satisfy the integer attempt equality.
        self.assertEqual(len(guard.rejections(self.source(run_attempt=True), self.args())), 1)

    def test_every_failed_condition_is_reported_with_the_final_error(self):
        source = self.source(
            status="in_progress", conclusion="success", event="schedule",
            path="other.yml", run_attempt=7, display_title="relink request=x parent=y",
            head_sha="ab" * 20,
        )
        code, out = self.run_main(source)
        self.assertEqual(code, 1)
        lines = out.splitlines()
        self.assertEqual(len(lines), 8)
        for line in lines[:-1]:
            self.assertTrue(line.startswith("checkpoint source 33994031370 rejected: "), line)
        self.assertEqual(
            lines[-1],
            "::error::local checkpoint source is not the exact terminal recovery"
            " attempt at this Linker commit",
        )

    def test_unreadable_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent.json"
            argv = [
                str(missing), "--run-id", "1", "--run-attempt", "1",
                "--request-id", REQUEST, "--parent-run-id", "2",
                "--parent-run-attempt", "1", "--head-sha", HEAD_SHA,
            ]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(guard.main(argv), 1)
            self.assertIn("run metadata is unreadable", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
