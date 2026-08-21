import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fingerprint_migration", ROOT / "ci/resolve_output_neutral_fingerprint_migration.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class OutputNeutralFingerprintMigrationTests(unittest.TestCase):
    def _paths(self, old="engine-v1", new="engine-v2", entries=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        baseline = root / "baseline.json"
        migrations = root / "migrations.json"
        baseline.write_text(json.dumps({"engine_fingerprint": old}), encoding="utf-8")
        entries = entries if entries is not None else [
            {"from": old, "to": new, "review": "reviewed"}
        ]
        migrations.write_text(
            json.dumps({"schema_version": 1, "migrations": entries}), encoding="utf-8"
        )
        return baseline, migrations

    def test_exact_reviewed_pair_emits_existing_adoption_contract(self):
        baseline, migrations = self._paths()
        self.assertEqual(MODULE.resolve(baseline, "engine-v2", migrations), "engine-v1::engine-v2")

    def test_no_drift_needs_no_adoption(self):
        baseline, migrations = self._paths(old="engine-v2", new="engine-v3")
        self.assertEqual(MODULE.resolve(baseline, "engine-v2", migrations), "")

    def test_unknown_drift_is_not_adopted(self):
        baseline, migrations = self._paths()
        self.assertEqual(MODULE.resolve(baseline, "engine-v3", migrations), "")

    def test_duplicate_pair_fails_closed(self):
        entries = [
            {"from": "engine-v1", "to": "engine-v2", "review": "one"},
            {"from": "engine-v1", "to": "engine-v2", "review": "two"},
        ]
        baseline, migrations = self._paths(entries=entries)
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            MODULE.resolve(baseline, "engine-v2", migrations)


if __name__ == "__main__":
    unittest.main()
