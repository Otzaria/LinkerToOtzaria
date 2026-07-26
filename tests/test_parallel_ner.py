import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from linker_artifact import BookKey  # noqa: E402
from link_books import claim_id  # noqa: E402
from ner_handoff import sha256_file, write_json_atomic  # noqa: E402
from parallel_ner import merge_bundles, rebind_bundle, split_plan  # noqa: E402


class ParallelNerTest(unittest.TestCase):
    def _inputs(self, root: Path):
        snapshot = root / "snapshot.db"
        connection = sqlite3.connect(snapshot)
        connection.execute(
            "CREATE TABLE lines_snapshot(source_name TEXT, canonical_he_title TEXT, "
            "line_index INTEGER, content TEXT, context_ref TEXT)"
        )
        rows = [
            ("s", f"book-{index}", 0, "word " * (index + 1), f"Book {index}:1")
            for index in range(4)
        ]
        connection.executemany("INSERT INTO lines_snapshot VALUES(?,?,?,?,?)", rows)
        connection.commit()
        connection.close()
        changed = [
            {
                "source_name": "s",
                "canonical_he_title": f"book-{index}",
                "ner_ranges": [],
            }
            for index in range(4)
        ]
        plan = {
            "schema_version": 2,
            "relink_request_id": "a" * 64,
            "snapshot_sha256": sha256_file(snapshot),
            "changelog_sha256": None,
            "engine_fingerprint": "engine",
            "changed": changed,
            "removed": [],
            "current_books": [
                {
                    "source_name": item["source_name"],
                    "canonical_he_title": item["canonical_he_title"],
                    "hash": f"{index:016x}",
                }
                for index, item in enumerate(changed)
            ],
        }
        plan_path = root / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        return snapshot, plan_path, plan

    def _bundle(self, root: Path, plan: dict, items: list[dict], index: int) -> Path:
        bundle = root / f"bundle-{index}"
        descriptors = []
        hashes = {
            (item["source_name"], item["canonical_he_title"]): item["hash"]
            for item in plan["current_books"]
        }
        for item in items:
            book = BookKey(item["source_name"], item["canonical_he_title"])
            relative = Path("ner-data") / claim_id(book) / "book_manifest.json"
            path = bundle / relative
            size, digest = write_json_atomic(path, {
                "schema_version": 2,
                "book": book.to_dict(),
                "source_book_hash": hashes[(book.source_name, book.canonical_he_title)],
                "line_count": 1,
                "eligible_line_count": 0,
                "ner_ranges": [],
                "batches": [],
            })
            descriptors.append({
                "book": book.to_dict(),
                "manifest_path": relative.as_posix(),
                "size": size,
                "sha256": digest,
            })
        write_json_atomic(bundle / "ner_manifest.json", {
            "schema_version": 2,
            "relink_request_id": plan["relink_request_id"],
            "snapshot_sha256": plan["snapshot_sha256"],
            "engine_fingerprint": plan["engine_fingerprint"],
            "batch_lines": 75,
            "books": descriptors,
        })
        return bundle

    def test_split_is_balanced_exact_disjoint_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot, plan_path, _plan = self._inputs(root)
            manifest = split_plan(snapshot, plan_path, root / "shards", 2)
            self.assertEqual(manifest["book_count"], 4)
            shard_plans = [
                json.loads((root / "shards" / item["path"]).read_text())
                for item in manifest["shards"]
            ]
            keys = [
                (item["source_name"], item["canonical_he_title"])
                for shard in shard_plans
                for item in shard["changed"]
            ]
            self.assertEqual(len(keys), len(set(keys)))
            self.assertEqual(len(keys), 4)

    def test_merge_reorders_and_verifies_disjoint_bundles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot, plan_path, plan = self._inputs(root)
            first = self._bundle(root, plan, plan["changed"][::2], 0)
            second = self._bundle(root, plan, plan["changed"][1::2], 1)
            output = root / "merged"
            count = merge_bundles(
                snapshot,
                plan_path,
                [second, first],
                output,
                expected_batch_lines=75,
            )
            self.assertEqual(count, 4)
            manifest = json.loads((output / "ner_manifest.json").read_text())
            self.assertEqual(
                [item["book"]["canonical_he_title"] for item in manifest["books"]],
                [f"book-{index}" for index in range(4)],
            )

    def test_rebind_requires_exact_rows_and_rewrites_run_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_snapshot, old_plan_path, old_plan = self._inputs(root)
            old_bundle = self._bundle(root, old_plan, old_plan["changed"], 0)

            new_snapshot = root / "new-snapshot.db"
            connection = sqlite3.connect(new_snapshot)
            connection.execute(
                "CREATE TABLE lines_snapshot(source_name TEXT, canonical_he_title TEXT, "
                "line_index INTEGER, content TEXT, context_ref TEXT)"
            )
            old_connection = sqlite3.connect(old_snapshot)
            connection.executemany(
                "INSERT INTO lines_snapshot VALUES(?,?,?,?,?)",
                old_connection.execute("SELECT * FROM lines_snapshot"),
            )
            old_connection.close()
            connection.execute(
                "INSERT INTO lines_snapshot VALUES(?,?,?,?,?)",
                ("other", "extra", 0, "extra text", "Extra 1"),
            )
            connection.commit()
            connection.close()

            new_plan = dict(old_plan)
            new_plan["relink_request_id"] = "b" * 64
            new_plan["snapshot_sha256"] = sha256_file(new_snapshot)
            new_plan["changed"] = old_plan["changed"] + [{
                "source_name": "other",
                "canonical_he_title": "extra",
                "ner_ranges": [],
            }]
            new_plan["current_books"] = old_plan["current_books"] + [{
                "source_name": "other",
                "canonical_he_title": "extra",
                "hash": "f" * 16,
            }]
            new_plan_path = root / "new-plan.json"
            new_plan_path.write_text(json.dumps(new_plan), encoding="utf-8")
            output = root / "rebound"

            count = rebind_bundle(
                old_snapshot,
                old_plan_path,
                old_bundle,
                new_snapshot,
                new_plan_path,
                output,
                expected_batch_lines=75,
            )

            self.assertEqual(count, 4)
            manifest = json.loads((output / "ner_manifest.json").read_text())
            self.assertEqual(manifest["relink_request_id"], "b" * 64)
            self.assertEqual(manifest["snapshot_sha256"], sha256_file(new_snapshot))

            connection = sqlite3.connect(new_snapshot)
            connection.execute(
                "UPDATE lines_snapshot SET content='changed' "
                "WHERE canonical_he_title='book-0'"
            )
            connection.commit()
            connection.close()
            new_plan["snapshot_sha256"] = sha256_file(new_snapshot)
            new_plan_path.write_text(json.dumps(new_plan), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "snapshot rows differ"):
                rebind_bundle(
                    old_snapshot,
                    old_plan_path,
                    old_bundle,
                    new_snapshot,
                    new_plan_path,
                    root / "must-fail",
                    expected_batch_lines=75,
                )


if __name__ == "__main__":
    unittest.main()
