import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from linker_artifact import (  # noqa: E402
    BookKey,
    LinkRecord,
    book_key_to_relpath,
    content_hash,
    read_artifact,
    validate_record,
    write_artifact,
)

EXAMPLE = os.path.join(ROOT, "examples", "MoreBooks", "חזון איש.jsonl")
SCHEMA = os.path.join(ROOT, "schema", "artifact.schema.json")


class ArtifactContractTest(unittest.TestCase):
    def test_example_loads_and_validates(self):
        recs = list(read_artifact(EXAMPLE))
        self.assertEqual(len(recs), 2)
        r0 = recs[0]
        self.assertEqual(r0.book_key, BookKey("MoreBooks", "חזון איש"))
        self.assertEqual(r0.target_ref, "Psalms 16:8")
        self.assertEqual((r0.start, r0.end), (37, 48))
        # all records in the file share one book_key
        self.assertEqual({r.book_key for r in recs}, {BookKey("MoreBooks", "חזון איש")})

    def test_example_matches_json_schema_if_available(self):
        try:
            import jsonschema  # type: ignore
        except ImportError:
            self.skipTest("jsonschema not installed")
        with open(SCHEMA, encoding="utf-8") as fh:
            schema = json.load(fh)
        with open(EXAMPLE, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    jsonschema.validate(json.loads(line), schema)

    def test_roundtrip(self):
        rec = LinkRecord(BookKey("Sefaria", 'שו"ת'), line_index=3, start=1, end=5, target_ref="Genesis 1:1")
        back = LinkRecord.from_dict(rec.to_dict())
        self.assertEqual(rec, back)

    def test_roundtrip_with_source_hash(self):
        rec = LinkRecord(BookKey("s", "t"), line_index=3, start=1, end=5,
                         target_ref="Genesis 1:1", source_hash=content_hash("abc"))
        back = LinkRecord.from_dict(rec.to_dict())
        self.assertEqual(rec, back)
        self.assertEqual(back.source_hash, content_hash("abc"))

    def test_roundtrip_with_validated_relative_context(self):
        rec = LinkRecord(
            BookKey("s", "t"), 3, 1, 5, "Genesis 1:1",
            source_hash=content_hash("abc"),
            context_ref="בראשית א, ב",
            relative_direction="above",
        )
        self.assertEqual(LinkRecord.from_dict(rec.to_dict()), rec)

    def test_content_hash_contract(self):
        # These constants are asserted identically in the Kotlin importer's test
        # (GenerateLinkerLinksTest.contentHashMatchesPythonContract) — the guard only works
        # if both sides compute the exact same digest.
        self.assertEqual(content_hash("hello"), "aaf4c61ddcc5e8a2")
        self.assertEqual(content_hash("שלום עולם"), "643cbc0fbf2800d7")
        self.assertEqual(content_hash(""), "da39a3ee5e6b4b0d")
        self.assertRegex(content_hash("anything"), r"^[0-9a-f]{16}$")

    def test_relpath_is_deterministic_and_safe(self):
        p = book_key_to_relpath(BookKey("MoreBooks", "חזון איש"))
        self.assertEqual(p, os.path.join("artifacts", "MoreBooks", "חזון איש.jsonl"))
        # unsafe chars get neutralised; result stays inside artifacts/<source>/
        p2 = book_key_to_relpath(BookKey("Sefaria", 'a/b:c"d'))
        self.assertEqual(p2, os.path.join("artifacts", "Sefaria", "a_b_c_d.jsonl"))

    def test_validate_rejects_bad_records(self):
        bad = [
            {"book_key": {"source_name": "", "canonical_he_title": "x"}, "line_index": 0, "start": 0, "end": 1, "target_ref": "R"},
            {"book_key": {"source_name": "s", "canonical_he_title": "x"}, "line_index": -1, "start": 0, "end": 1, "target_ref": "R"},
            {"book_key": {"source_name": "s", "canonical_he_title": "x"}, "line_index": 0, "start": 5, "end": 5, "target_ref": "R"},
            {"book_key": {"source_name": "s", "canonical_he_title": "x"}, "line_index": 0, "start": 0, "end": 1, "target_ref": ""},
            {"book_key": {"source_name": "s", "canonical_he_title": "x"}, "line_index": 0, "line_index_base": 1, "start": 0, "end": 1, "target_ref": "R"},
            # unknown top-level field (schema is additionalProperties: false)
            {"book_key": {"source_name": "s", "canonical_he_title": "x"}, "line_index": 0, "start": 0, "end": 1, "target_ref": "R", "bogus": 1},
            # unknown book_key field
            {"book_key": {"source_name": "s", "canonical_he_title": "x", "z": 1}, "line_index": 0, "start": 0, "end": 1, "target_ref": "R"},
            # source_path wrong type
            {"book_key": {"source_name": "s", "canonical_he_title": "x"}, "line_index": 0, "start": 0, "end": 1, "target_ref": "R", "source_path": 5},
            # source_hash wrong length
            {"book_key": {"source_name": "s", "canonical_he_title": "x"}, "line_index": 0, "start": 0, "end": 1, "target_ref": "R", "source_hash": "abc"},
            # source_hash non-hex
            {"book_key": {"source_name": "s", "canonical_he_title": "x"}, "line_index": 0, "start": 0, "end": 1, "target_ref": "R", "source_hash": "ZZZZZZZZZZZZZZZZ"},
            {"book_key": {"source_name": "s", "canonical_he_title": "x"}, "line_index": 0, "start": 0, "end": 1, "target_ref": "R", "context_ref": "בראשית א"},
            {"book_key": {"source_name": "s", "canonical_he_title": "x"}, "line_index": 0, "start": 0, "end": 1, "target_ref": "R", "relative_direction": "sideways", "context_ref": "בראשית א"},
        ]
        for d in bad:
            with self.assertRaises(ValueError):
                validate_record(d)

    def test_write_asserts_single_book_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.jsonl")
            good = [
                LinkRecord(BookKey("s", "t"), 0, 0, 1, "A"),
                LinkRecord(BookKey("s", "t"), 1, 0, 1, "B"),
            ]
            self.assertEqual(write_artifact(path, good), 2)
            self.assertEqual(len(list(read_artifact(path))), 2)
            mixed = [LinkRecord(BookKey("s", "t"), 0, 0, 1, "A"), LinkRecord(BookKey("s", "u"), 0, 0, 1, "B")]
            with self.assertRaises(ValueError):
                write_artifact(path, mixed)


if __name__ == "__main__":
    unittest.main()
