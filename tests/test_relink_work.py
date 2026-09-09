"""relink_work.json — the run's work provenance, from producer to published asset.

Recovery 34021656701 adopted 5,126 of 5,127 books from run 34016397157 and computed
exactly ONE, in 7m12s. Its published relink_manifest.json was byte-shaped exactly like a
full relink's — identity, digests, engine fingerprint, and not one number about the work
— so release linker-release-sha256-4632e6fb… carries no record that 99.98% of its
content was computed by a different run. These tests pin the object that fixes that, the
independent validator that guards it, and the workflow that carries it onto the release.
"""

import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

import incremental  # noqa: E402
from line_baseline import build_line_baseline  # noqa: E402
from linker_artifact import BookKey, LinkRecord, book_key_to_relpath, write_artifact  # noqa: E402


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# The boundary validator is deliberately NOT imported from src/: it restates the schema
# so a producer bug cannot validate itself. Loading it by path is what binds the two.
contract = _load("validate_relink_work", "ci/validate_relink_work.py")
manifest_contract = _load("validate_relink_manifest", "ci/validate_relink_manifest.py")


def full_run(**overrides) -> dict:
    """A relink that computed everything it planned (the 33870190942 shape)."""
    values = dict(total=214, adopted=0, computed=214, failed=0, links_total=1464446)
    values.update(overrides)
    return incremental.build_work_provenance(**values)


def recovery(**overrides) -> dict:
    """Recovery 34021656701: 5126 books adopted from 34016397157, one computed."""
    values = dict(
        total=5127, adopted=5126, computed=1, failed=0, links_total=1464826,
        checkpoint_source="source-34016397157-1",
    )
    values.update(overrides)
    return incremental.build_work_provenance(**values)


class WorkProvenanceBuilderTest(unittest.TestCase):
    def test_full_run_reports_no_checkpoint_and_no_invented_ids(self):
        value = full_run()
        self.assertEqual(value["books_adopted"], 0)
        self.assertEqual(value["books_computed"], 214)
        self.assertIsNone(value["checkpoint_source"])
        self.assertIsNone(value["checkpoint_source_run_id"])
        self.assertIsNone(value["checkpoint_source_attempt"])
        self.assertIn(
            "books 214 = adopted 0 + computed 214 + failed 0, links 1464446, no checkpoint",
            incremental.format_work_provenance_line("run/relink_work.json", value),
        )

    def test_recovery_adopting_all_but_one_is_distinguishable_from_a_full_relink(self):
        value = recovery()
        self.assertEqual(
            (value["books_total"], value["books_adopted"], value["books_computed"]),
            (5127, 5126, 1),
        )
        self.assertEqual(value["checkpoint_source_run_id"], 34016397157)
        self.assertEqual(value["checkpoint_source_attempt"], 1)
        line = incremental.format_work_provenance_line("run/relink_work.json", value)
        self.assertIn(
            "books 5127 = adopted 5126 + computed 1 + failed 0, links 1464826, "
            "checkpoint from run 34016397157 attempt 1",
            line,
        )
        # The whole point: a reader can tell these two apart without reading the log.
        self.assertNotEqual(
            {k: v for k, v in value.items() if k != "links_total"},
            {k: v for k, v in full_run(total=5127, computed=5127).items() if k != "links_total"},
        )

    def test_requeues_reclaims_and_replacements_are_carried(self):
        # The 34016397157 shape, had L4/L5 been in place: one book requeued after a
        # MemoryError, books re-claimed from dead workers, bounded replacements spent.
        value = incremental.build_work_provenance(
            total=5127, adopted=0, computed=5126, failed=1, links_total=1464700,
            requeued=1, reclaimed=3, duplicates=0, workers_replaced=12,
            recycled_for_heavy=4,
        )
        self.assertEqual(value["books_requeued_after_memoryerror"], 1)
        self.assertEqual(value["books_reclaimed_after_worker_death"], 3)
        self.assertEqual(value["workers_replaced"], 12)
        self.assertEqual(value["workers_recycled_for_heavy"], 4)
        self.assertEqual(value["duplicates"], 0)
        contract.validate(value)

    def test_unrecognised_checkpoint_name_keeps_the_string_and_invents_no_ids(self):
        value = recovery(checkpoint_source="a-checkpoint-named-some-other-way")
        self.assertEqual(value["checkpoint_source"], "a-checkpoint-named-some-other-way")
        self.assertIsNone(value["checkpoint_source_run_id"])
        self.assertIsNone(value["checkpoint_source_attempt"])
        contract.validate(value)
        self.assertIn(
            "checkpoint from a-checkpoint-named-some-other-way",
            incremental.format_work_provenance_line("run/relink_work.json", value),
        )

    def test_a_named_checkpoint_that_adopted_nothing_is_a_real_state(self):
        # Attempt 34015274945 died with 280 shard files and ZERO completed books; the
        # next attempt restored and discarded it. `adopted == 0` must not imply
        # "no checkpoint" — that would hide the run that produced nothing.
        value = incremental.build_work_provenance(
            total=5127, adopted=0, computed=5127, failed=0, links_total=1464826,
            checkpoint_source="source-34015274945-1",
        )
        self.assertEqual(value["checkpoint_source_run_id"], 34015274945)
        self.assertEqual(value["books_adopted"], 0)
        contract.validate(value)

    def test_builder_output_round_trips_through_the_independent_validator(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in (full_run(), recovery()):
                path = incremental.write_work_provenance(tmp, value)
                self.assertEqual(contract.load(Path(path)), value)
                contract.validate(contract.load(Path(path)))


class SelfIdentificationTest(unittest.TestCase):
    """The asset answers "which run, and when?" when downloaded on its own.

    A second self-report of an identity relink_manifest.json also carries is only safe
    because it cannot drift: every CI call site of the validator holds this file to the
    same two coordinates ci/validate_relink_manifest.py holds the manifest to.
    """

    def test_a_ci_run_stamps_its_coordinates_and_the_callers_timestamp(self):
        value = recovery(generated_at="2026-09-06T07:12:44Z",
                         run_id=34021656701, run_attempt=1)
        self.assertEqual(value["generated_at"], "2026-09-06T07:12:44Z")
        self.assertEqual(value["relink_run_id"], 34021656701)
        self.assertEqual(value["relink_run_attempt"], 1)
        contract.validate(value, 34021656701, 1)

    def test_an_operator_run_off_a_runner_reports_nulls_rather_than_zeroes(self):
        value = full_run()
        for field in ("generated_at", "relink_run_id", "relink_run_attempt"):
            self.assertIsNone(value[field])
        contract.validate(value)  # accepted: nothing to state, nothing invented

    def test_run_coordinates_come_from_the_environment_as_a_pair(self):
        read = incremental.read_run_coordinates
        self.assertEqual(
            read({"GITHUB_RUN_ID": "34021656701", "GITHUB_RUN_ATTEMPT": "2"}),
            (34021656701, 2),
        )
        for environment in (
            {},                                                        # local run
            {"GITHUB_RUN_ID": "34021656701"},                          # half a coordinate
            {"GITHUB_RUN_ATTEMPT": "1"},
            {"GITHUB_RUN_ID": "34021656701", "GITHUB_RUN_ATTEMPT": "0"},
            {"GITHUB_RUN_ID": "0", "GITHUB_RUN_ATTEMPT": "1"},
            {"GITHUB_RUN_ID": "1e9", "GITHUB_RUN_ATTEMPT": "1"},       # not a plain int
            {"GITHUB_RUN_ID": " 34021656701", "GITHUB_RUN_ATTEMPT": "1"},
            {"GITHUB_RUN_ID": "9" * 19, "GITHUB_RUN_ATTEMPT": "1"},    # unbounded
        ):
            with self.subTest(environment=environment):
                self.assertEqual(read(environment), (None, None))

    def test_the_driver_stamps_them_from_the_environment_and_generated_at(self):
        source = (ROOT / "src/incremental.py").read_text(encoding="utf-8")
        step = source.split("# 6. Record what this run actually DID")[1]
        self.assertIn("read_run_coordinates()", step)
        self.assertIn('generated_at=getattr(args, "generated_at", None)', step)


class WorkProvenanceWriterTest(unittest.TestCase):
    def test_writes_canonical_json_with_one_trailing_lf_into_the_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run" / "nested"
            path = Path(incremental.write_work_provenance(str(run_dir), recovery()))
            self.assertEqual(path, run_dir / "relink_work.json")
            raw = path.read_bytes()
            self.assertEqual(
                raw,
                json.dumps(recovery(), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode() + b"\n",
            )
            self.assertNotIn(b"\r", raw)
            self.assertEqual([p.name for p in run_dir.iterdir()], ["relink_work.json"])

    def test_a_previous_runs_provenance_cannot_survive_into_this_one(self):
        # The run dir IS the durable local checkpoint and persists between runs on the
        # self-hosted host. run_incremental unlinks the file before it does any work, so
        # a file present at the end can only have been written by this run.
        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp) / "relink_work.json"
            stale.write_text("stale\n", encoding="utf-8")
            source = (ROOT / "src/incremental.py").read_text(encoding="utf-8")
            self.assertIn('os.remove(os.path.join(args.run_dir, "relink_work.json"))', source)
            path = incremental.write_work_provenance(tmp, full_run())
            self.assertEqual(contract.load(Path(path))["books_total"], 214)


class WorkProvenanceValidatorTest(unittest.TestCase):
    def write(self, root, value=None, raw=None) -> Path:
        path = Path(root) / "relink_work.json"
        value = recovery() if value is None else value
        path.write_bytes(
            raw if raw is not None
            else (json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")) + "\n").encode()
        )
        return path

    def test_rejects_book_arithmetic_that_does_not_add_up(self):
        value = recovery()
        value["books_computed"] = 2  # 5126 + 2 + 0 != 5127
        with self.assertRaises(ValueError) as caught:
            contract.validate(value)
        self.assertIn("!= books_total", str(caught.exception))

    def test_rejects_negative_and_boolean_counters(self):
        for field in contract.COUNTERS:
            for bad in (-1, True):
                value = recovery()
                value[field] = bad
                # Keep the arithmetic satisfiable so the type/range check is what fires.
                if field in ("books_total", "books_adopted", "books_computed", "books_failed"):
                    with self.assertRaises(ValueError):
                        contract.validate(value)
                else:
                    with self.assertRaises(ValueError) as caught:
                        contract.validate(value)
                    self.assertIn(field, str(caught.exception))

    def test_rejects_missing_and_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = recovery()
            del missing["links_total"]
            with self.assertRaises(ValueError):
                contract.load(self.write(tmp, missing))
            extra = recovery()
            extra["books_relinked"] = 5127
            with self.assertRaises(ValueError):
                contract.load(self.write(tmp, extra))

    def test_rejects_non_canonical_bytes_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            canonical = json.dumps(recovery(), ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"))
            for raw in (canonical.encode(),                       # no trailing LF
                        (canonical + "\n\n").encode(),            # two
                        json.dumps(recovery(), sort_keys=True).encode() + b"\n",  # spaced
                        (canonical + "\r\n").encode()):           # CRLF
                with self.assertRaises(ValueError):
                    contract.load(self.write(tmp, raw=raw))
            duplicate = canonical[:-1] + ',"books_total":5127}\n'
            with self.assertRaises(ValueError) as caught:
                contract.load(self.write(tmp, raw=duplicate.encode()))
            self.assertIn("duplicate", str(caught.exception))

    def test_rejects_a_half_empty_or_unnamed_checkpoint_source(self):
        value = recovery()
        value["checkpoint_source_attempt"] = None
        with self.assertRaises(ValueError):
            contract.validate(value)
        value = recovery()
        value["checkpoint_source"] = None
        with self.assertRaises(ValueError) as caught:
            contract.validate(value)
        self.assertIn("without a checkpoint_source", str(caught.exception))
        value = recovery()
        value["checkpoint_source_run_id"] = 0
        with self.assertRaises(ValueError):
            contract.validate(value)

    def test_rejects_an_unbounded_or_non_printable_checkpoint_name(self):
        for bad in ("source-1-1\nGITHUB_ENV=owned", "s" * 201, ""):
            value = recovery(checkpoint_source=bad) if bad else recovery()
            if not bad:
                value["checkpoint_source"] = ""
            with self.assertRaises(ValueError):
                contract.validate(value)

    def test_rejects_a_schema_version_it_does_not_know(self):
        for bad in (0, 2, True, "1"):
            value = recovery()
            value["schema_version"] = bad
            with self.assertRaises(ValueError):
                contract.validate(value)

    def test_rejects_a_generated_at_that_is_not_the_shape_the_workflow_emits(self):
        # relink.yml passes `date -u +%Y-%m-%dT%H:%M:%SZ`, the same string meta.json
        # records. Pinned exactly so the two cannot describe one instant differently.
        for bad in ("2026-09-06T07:12:44+00:00", "2026-09-06 07:12:44Z",
                    "2026-09-06T07:12:44.5Z", "2026-09-06T07:12:44Z ", "", 20260906):
            with self.subTest(generated_at=bad):
                value = recovery()
                value["generated_at"] = bad
                with self.assertRaises(ValueError):
                    contract.validate(value)

    def test_rejects_half_empty_or_impossible_run_coordinates(self):
        for run_id, attempt in ((34021656701, None), (None, 1), (0, 1), (1, 0),
                                (True, 1), ("34021656701", 1)):
            with self.subTest(run_id=run_id, attempt=attempt):
                value = recovery()
                value["relink_run_id"] = run_id
                value["relink_run_attempt"] = attempt
                with self.assertRaises(ValueError):
                    contract.validate(value)

    def test_rejects_coordinates_that_disagree_with_the_callers_own(self):
        # This is what makes a second self-report of the identity safe: the publisher
        # states the run it is publishing, and a file describing another one dies here.
        value = recovery(run_id=34021656701, run_attempt=1)
        contract.validate(value, 34021656701, 1)
        for expected_id, expected_attempt in ((34016397157, 1), (34021656701, 2)):
            with self.subTest(expected=(expected_id, expected_attempt)):
                with self.assertRaises(ValueError) as caught:
                    contract.validate(value, expected_id, expected_attempt)
                self.assertIn("expected", str(caught.exception))
        # A file with no coordinates at all cannot satisfy a caller that states them.
        with self.assertRaises(ValueError):
            contract.validate(recovery(), 34021656701, 1)

    def test_cli_exits_nonzero_on_a_bad_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = recovery()
            bad["books_failed"] = 7
            path = self.write(tmp, bad)
            result = subprocess.run(
                [sys.executable, str(ROOT / "ci/validate_relink_work.py"), str(path)],
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            good = self.write(tmp)
            result = subprocess.run(
                [sys.executable, str(ROOT / "ci/validate_relink_work.py"), str(good)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

    def test_cli_holds_the_file_to_the_run_the_caller_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, recovery(run_id=34021656701, run_attempt=1))

            def run(*flags):
                return subprocess.run(
                    [sys.executable, str(ROOT / "ci/validate_relink_work.py"), str(path),
                     *flags],
                    capture_output=True, text=True,
                )

            self.assertEqual(run("--run-id", "34021656701", "--run-attempt", "1").returncode, 0)
            self.assertNotEqual(run("--run-id", "34016397157", "--run-attempt", "1").returncode, 0)
            self.assertNotEqual(run("--run-id", "34021656701", "--run-attempt", "2").returncode, 0)


class LinkTotalTest(unittest.TestCase):
    """links_total comes from the pass that already digests the whole store."""

    def build(self, root: Path, books: dict) -> int:
        snapshot = root / "snapshot.db"
        connection = sqlite3.connect(snapshot)
        connection.execute(
            "CREATE TABLE lines_snapshot(source_name TEXT, canonical_he_title TEXT, "
            "line_index INTEGER, content TEXT, context_ref TEXT)"
        )
        hashes = {}
        for (source, title), records in books.items():
            connection.execute(
                "INSERT INTO lines_snapshot VALUES(?,?,?,?,?)",
                (source, title, 0, "בדיקה", title),
            )
            hashes[(source, title)] = "a" * 16
            if records:
                write_artifact(str(root / book_key_to_relpath(BookKey(source, title))), records)
        connection.commit()
        connection.close()
        return build_line_baseline(
            str(snapshot),
            str(root / "line-baseline"),
            current_hashes=hashes,
            snapshot_sha256="b" * 64,
            engine_fingerprint="engine=test",
            artifacts_root=str(root / "artifacts"),
        )

    def records(self, source, title, count):
        return [
            LinkRecord(book_key=BookKey(source, title), line_index=0, start=index,
                       end=index + 1, target_ref=f"Psalms {index + 1}:1")
            for index in range(count)
        ]

    def test_counts_every_record_in_the_published_store_including_untouched_books(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            total = self.build(root, {
                ("Sefaria", "ספר א"): self.records("Sefaria", "ספר א", 3),
                ("Sefaria", "ספר ב"): self.records("Sefaria", "ספר ב", 139),
                ("Sefaria", "ספר ג"): [],  # zero links → no file at all
            })
            self.assertEqual(total, 142)

    def test_an_empty_store_reports_zero_rather_than_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(self.build(root, {("Sefaria", "ספר א"): []}), 0)


class ReleaseCarriesTheWorkObjectTest(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/relink.yml").read_text(encoding="utf-8")
        self.unpack = (ROOT / "ci/unpack_publisher_handoff.py").read_text(encoding="utf-8")

    def test_both_producers_lift_the_drivers_file_and_validate_it(self):
        self.assertEqual(self.workflow.count("- name: Collect relink work provenance"), 2)
        self.assertEqual(
            self.workflow.count('cp "$RUN_DIR/relink_work.json" relink_work.json'), 2)
        # Producer-side validation (x2) plus the publisher's strict boundary check.
        self.assertEqual(
            self.workflow.count("python3 ci/validate_relink_work.py"), 3)
        self.assertIn("python3 ci/validate_relink_work.py handoff/relink_work.json",
                      self.workflow)
        self.assertIn("test -f handoff/relink_work.json", self.workflow)

    def test_every_validator_call_site_pins_the_run_it_is_publishing(self):
        # The work object self-identifies, so it must be held to the SAME coordinates
        # ci/validate_relink_manifest.py holds relink_manifest.json to — otherwise two
        # files on one release could name two different runs.
        coordinates = '--run-id "$GITHUB_RUN_ID" --run-attempt "$GITHUB_RUN_ATTEMPT"'
        # 3 work validators + the publisher's manifest validator.
        self.assertEqual(self.workflow.count(coordinates), 4)
        for call in ("python3 ci/validate_relink_work.py relink_work.json \\\n"
                     "            " + coordinates,
                     "python3 ci/validate_relink_work.py handoff/relink_work.json \\\n"
                     "            " + coordinates):
            self.assertIn(call, self.workflow)
        self.assertIn("ci/validate_relink_manifest.py handoff/relink_manifest.json",
                      self.workflow)

    def test_the_handoff_tar_carries_it_and_the_unpacker_requires_it(self):
        self.assertEqual(
            self.workflow.count(
                "tar -cf publisher_handoff.tar linker_links.zst linker_links.zst.sha256 "
                "relink_manifest.json relink_work.json baseline meta.json"),
            2,
        )
        self.assertIn('"relink_work.json", "baseline", "meta.json"', self.unpack)
        self.assertEqual(self.unpack.count("relink_work.json"), 2)  # allowed_roots + required

    def test_all_three_release_asset_sites_moved_together(self):
        # The first DERIVES the content-addressed tag, the second rejects any asset
        # outside the set, the third proves the remote descriptors. A site left behind
        # would either drop the asset or fail the release as "unexpected".
        self.assertEqual(
            self.workflow.count(
                'names=("linker_links.zst","meta.json","relink_manifest.json","relink_work.json")'),
            3,
        )
        self.assertNotIn(
            'names=("linker_links.zst","meta.json","relink_manifest.json")', self.workflow)

    def test_the_output_pointer_release_still_carries_exactly_two_assets(self):
        # SeforimLibrary's recovery guard asserts
        # names=={"linker_release_pointer.json","relink_manifest.json"} on this exact
        # release (.github/workflows/manual-generate-release.yml, the "recovery Linker
        # output release identity/assets differ" check). Adding a third asset HERE
        # breaks the recovery path in that repository, which is why the work object
        # rides the linker-release-sha256-* release instead.
        self.assertIn(
            '"$GITHUB_SHA" output-pointer/linker_release_pointer.json '
            "output-pointer/relink_manifest.json",
            self.workflow,
        )
        pointer_step = self.workflow.split("- name: Expose exact payload pointer")[1]
        pointer_step = pointer_step.split("- name: Commit baseline")[0]
        self.assertNotIn("relink_work.json", pointer_step)

    def test_an_unpublished_payload_keeps_its_provenance(self):
        # L1's preservation path: a cancel between packing and publishing keeps the
        # payload; without this it would keep bytes nobody can explain.
        self.assertEqual(
            self.workflow.count(
                "for provenance in relink_manifest.json relink_work.json; do"), 2)


class PublisherHandoffUnpackerTest(unittest.TestCase):
    """The publisher's FIRST gate, exercised for real rather than by grep.

    ci/unpack_publisher_handoff.py both rejects members it does not expect and demands
    the ones it does. Adding an asset to the release without adding it here would ship a
    release whose fourth asset silently vanished somewhere in the tar.
    """

    ROOTS = ("linker_links.zst", "linker_links.zst.sha256", "relink_manifest.json",
             "relink_work.json", "meta.json")

    def unpack(self, root: Path, names, extra=()):
        payload = root / "payload"
        payload.mkdir()
        for name in names:
            (payload / name).write_text(name, encoding="utf-8")
        for name in extra:
            (payload / name).write_text("surprise", encoding="utf-8")
        (payload / "baseline").mkdir()
        (payload / "baseline" / "snapshot_hashes.json").write_text("{}", encoding="utf-8")
        archive = root / "publisher_handoff.tar"
        with tarfile.open(archive, "w") as bundle:
            for entry in sorted(payload.iterdir()):
                bundle.add(entry, arcname=entry.name)
        sidecar = root / "publisher_handoff.tar.sha256"
        sidecar.write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "\n",
                           encoding="ascii")
        return subprocess.run(
            [sys.executable, str(ROOT / "ci/unpack_publisher_handoff.py"),
             str(archive), str(sidecar), str(root / "handoff")],
            capture_output=True, text=True,
        )

    def test_a_complete_handoff_extracts_the_work_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.unpack(Path(tmp), self.ROOTS)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(tmp) / "handoff/relink_work.json").is_file())

    def test_a_handoff_without_the_work_object_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            names = [name for name in self.ROOTS if name != "relink_work.json"]
            result = self.unpack(Path(tmp), names)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing relink_work.json", result.stderr)

    def test_the_allow_list_was_widened_by_exactly_one_name(self):
        # Proof that relink_work.json is allowed BY NAME, not by a loosened rule.
        with tempfile.TemporaryDirectory() as tmp:
            result = self.unpack(Path(tmp), self.ROOTS, extra=("relink_work.json.bak",))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unexpected publisher handoff member", result.stderr)


class RelinkManifestStaysFrozenTest(unittest.TestCase):
    def test_the_manifest_key_set_is_still_the_one_another_repo_pins_exactly(self):
        """Why relink_work.json is a separate file rather than a key in the manifest.

        SeforimLibrary (branch otzaria, .github/workflows/manual-generate-release.yml,
        Phase-2 step "Fetch and verify the Linker payload") does:

            if set(m) != {"schema_version", "sefaria_tag", "snapshot_zst_sha256",
                          "engine_fingerprint", "payload_sha256", "linker_commit",
                          "relink_run_id", "relink_run_attempt", "relink_request_id",
                          "parent_run_id", "parent_run_attempt"}:
                sys.exit(f"relink manifest key set mismatch: {sorted(m)}")

        An EXACT key set in a different repository: one added key fails every build
        until both repos land together — and a recovery must be able to run at the same
        head_sha as its checkpoint source, so they cannot land together mid-cycle.
        """
        self.assertEqual(
            manifest_contract.KEYS,
            {"schema_version", "sefaria_tag", "snapshot_zst_sha256", "engine_fingerprint",
             "payload_sha256", "linker_commit", "relink_run_id", "relink_run_attempt",
             "relink_request_id", "parent_run_id", "parent_run_attempt"},
        )
        workflow = (ROOT / ".github/workflows/relink.yml").read_text(encoding="utf-8")
        self.assertEqual(workflow.count('"schema_version": 2,'), 2)
        self.assertNotIn("relink_work", workflow.split("- name: Emit relink manifest")[1]
                         .split("- name: Collect relink work provenance")[0])


if __name__ == "__main__":
    unittest.main()
