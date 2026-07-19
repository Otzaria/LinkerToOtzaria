import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "ci" / "validate_relink_manifest.py"
SPEC = importlib.util.spec_from_file_location("validate_relink_manifest", SCRIPT)
contract = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(contract)


class RelinkManifestTest(unittest.TestCase):
    def value(self):
        return {
            "schema_version": 2,
            "sefaria_tag": "export-v1",
            "snapshot_zst_sha256": "1" * 64,
            "engine_fingerprint": "engine=test;policy=drop",
            "payload_sha256": "2" * 64,
            "linker_commit": "3" * 40,
            "relink_run_id": 10,
            "relink_run_attempt": 2,
            "relink_request_id": "4" * 64,
            "parent_run_id": 20,
            "parent_run_attempt": 1,
        }

    def args(self):
        v = self.value()
        return argparse.Namespace(
            payload_sha256=v["payload_sha256"], linker_commit=v["linker_commit"],
            run_id=10, run_attempt=2, request_id=v["relink_request_id"],
            parent_run_id=20, parent_run_attempt=1,
        )

    def write(self, root, value=None, raw=None):
        path = root / "manifest.json"
        value = self.value() if value is None else value
        path.write_bytes(raw if raw is not None else (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())
        return path

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = contract.load(self.write(Path(tmp)))
            contract.validate(value, self.args())

    def test_duplicate_and_boolean_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = json.dumps(self.value(), sort_keys=True, separators=(",", ":"))[:-1] + ',"schema_version":2}\n'
            with self.assertRaises(ValueError): contract.load(self.write(Path(tmp), raw=raw.encode()))
            value = self.value(); value["relink_run_attempt"] = True
            with self.assertRaises(ValueError): contract.validate(value, self.args())

    def test_control_character_and_identity_drift_are_rejected(self):
        value = self.value(); value["engine_fingerprint"] = "ok\nGITHUB_ENV=bad"
        with self.assertRaises(ValueError): contract.validate(value, self.args())
        value = self.value(); value["relink_run_id"] = 11
        with self.assertRaises(ValueError): contract.validate(value, self.args())


if __name__ == "__main__":
    unittest.main()
