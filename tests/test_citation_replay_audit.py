"""A long replay must attest the loaded engine, not a later file revision."""
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('citation_replay_audit', ROOT / 'scripts/compare_citation_replay.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class ReplayIdentityTest(unittest.TestCase):
    def test_replacing_the_source_cannot_stamp_old_output_as_the_new_engine(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'engine.py'
            original = b'def quiet_sefaria_linker_logs(): pass\nvalue = 1\n'
            updated = original.replace(b'value = 1', b'value = 2')
            path.write_bytes(original)
            first = audit.load_engine('citation_fixture_engine', path)
            self.addCleanup(sys.modules.pop, 'citation_fixture_engine', None)
            path.write_bytes(updated)
            self.assertEqual(first.value, 1)
            self.assertEqual(first._audit_source_sha256, hashlib.sha256(original).hexdigest())
            second = audit.load_engine('citation_fixture_engine', path)
            self.assertEqual(second.value, 2)
            self.assertEqual(second._audit_source_sha256, hashlib.sha256(updated).hexdigest())
            self.assertNotEqual(first._audit_source_sha256, second._audit_source_sha256)


if __name__ == '__main__':
    unittest.main()
