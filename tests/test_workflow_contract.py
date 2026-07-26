import hashlib
import json
from pathlib import Path
import unittest


class RelinkWorkflowContractTest(unittest.TestCase):
    def test_server_host_lease_is_self_provisioning(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/relink.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Bootstrap durable cross-repo host lease", workflow)
        self.assertIn("if: inputs.target != 'kaggle'", workflow)
        self.assertIn("bash ci/bootstrap_host_lock.sh ci/otzaria-pipeline.tmpfiles.conf", workflow)

    def test_content_addressed_draft_is_resumable_without_clobber(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/relink.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('if ! gh release create "$tag" --draft --target "$GITHUB_SHA"', workflow)
        self.assertIn('gh release view "$tag" >/dev/null 2>&1 || exit 1', workflow)
        self.assertIn('gh release upload "$tag" "handoff/$name"', workflow)
        self.assertIn("existing immutable draft asset differs from handoff bytes", workflow)
        self.assertNotIn('gh release upload "$tag" "handoff/$name" --clobber', workflow)

    def test_artifact_restore_uses_version_independent_rest_and_safe_tar(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/relink.yml").read_text(
            encoding="utf-8"
        )
        segment = workflow.split("- name: Restore artifact store from the latest release", 1)[1]
        segment = segment.split("- name: Resolve upstream tags", 1)[0]
        self.assertIn("gh api --paginate -X GET", segment)
        self.assertIn("releases/assets/$asset_id", segment)
        self.assertIn('[[ "$remote_digest" =~ ^sha256:', segment)
        self.assertIn('filter="data"', segment)
        self.assertIn('path.parts[0] not in {"artifacts", "line-baseline"}', segment)
        self.assertIn('member.name == "meta.json" and member.isfile()', segment)
        self.assertIn('path.suffix != ".jsonl"', segment)
        self.assertIn('path.name in {".DS_Store", ".gitkeep"}', segment)
        self.assertIn('path.name.startswith("._")', segment)
        self.assertIn("members=artifact_members", segment)
        self.assertNotIn("if: inputs.target != 'kaggle'", segment)
        self.assertNotIn('latest_tag="$(gh release view', segment)

    def test_attested_fingerprint_adoption_reaches_serial_recovery(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/relink.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('PLAN_ARGS+=(--adopt-fingerprint "$ADOPT_FINGERPRINT")', workflow)
        self.assertIn('ARGS+=(--adopt-fingerprint "$ADOPT_FINGERPRINT")', workflow)
        self.assertGreaterEqual(workflow.count("--forbid-full-relink"), 2)

    def test_kaggle_is_ner_only_and_resolution_runs_on_server(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/relink.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("export LINKER_STACK_ROLE=ner", workflow)
        self.assertIn("src/precompute_ner.py", workflow)
        self.assertIn("--workers 2", workflow)
        self.assertIn("name: Resolve raw NER on the durable CPU host", workflow)
        self.assertIn("runs-on: [self-hosted, Linux, ARM64, server-2]", workflow)
        self.assertIn("LINKER_STACK_ROLE: resolver", workflow)
        self.assertIn("--ner-bundle-dir", workflow)
        self.assertIn("--engine-workers 2", workflow)
        self.assertIn("LINKER_BATCH_LINES: 25", workflow)
        self.assertIn("flock -w 3600 9", workflow)

    def test_serial_kaggle_timeout_fits_ephemeral_session(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/relink.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("inputs.target == 'kaggle' && 90", workflow)
        self.assertIn("--deadline-seconds 3600", workflow)
        self.assertIn("compression-level: 0", workflow)
        self.assertIn("Pack resumable NER checkpoint after bounded failure", workflow)
        self.assertIn("Restore exact prior-attempt NER checkpoint", workflow)
        self.assertIn("Kaggle now performs NER only", workflow)

    def test_line_baseline_seed_never_starts_the_linker(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/relink.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("seed_line_baseline:", workflow)
        self.assertIn("ci/seed_line_baseline.py", workflow)
        seed = workflow.index('if [ -n "$SEED_LINE_BASELINE" ]; then', workflow.index("id: compute"))
        setup = workflow.index("bash ci/setup_stack.sh 9>&-", seed)
        self.assertLess(seed, setup)
        segment = workflow[seed:setup]
        self.assertIn("exit 0", segment)
        self.assertNotIn("src/incremental.py", segment)

    def test_resolver_only_recovery_uses_exact_producer_artifact(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/relink.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("raw_ner_source_run_id:", workflow)
        self.assertIn("raw_ner_source_run_attempt:", workflow)
        self.assertIn("Validate exact raw-NER recovery source", workflow)
        self.assertIn("raw-ner-handoff-{0}-{1}", workflow)
        self.assertIn("run-id: ${{ inputs.raw_ner_source_run_id || github.run_id }}", workflow)
        self.assertIn("needs.relink.outputs.raw_ner_artifact_name", workflow)
        self.assertIn(
            'repos/$GITHUB_REPOSITORY/compare/${SOURCE_HEAD}...${GITHUB_SHA}',
            workflow,
        )
        self.assertIn(".merge_base_commit.sha == $source", workflow)
        self.assertIn(
            '--jq ".artifacts[] | select(.name == \\"$artifact_name\\"',
            workflow,
        )
        self.assertNotIn(
            '--jq ".artifacts[] | select(.name == \\\\\\"$artifact_name\\\\\\"',
            workflow,
        )

    def test_arm_resolver_uses_the_verified_kaggle_runtime_lock(self):
        root = Path(__file__).parents[1]
        setup = (root / "ci/setup_stack.sh").read_text(encoding="utf-8")
        manifest = json.loads(
            (root / "ci/runtime-lock/runtime-manifest.json").read_text(encoding="utf-8")
        )
        freeze = root / "ci/runtime-lock/sefaria.txt"
        self.assertEqual(
            hashlib.sha256(freeze.read_bytes()).hexdigest(),
            manifest["sefaria_freeze_sha256"],
        )
        combined = hashlib.sha256(
            (
                manifest["sefaria_freeze_sha256"]
                + "\n"
                + manifest["gpu_server_freeze_sha256"]
                + "\n"
            ).encode()
        ).hexdigest()[:16]
        self.assertEqual(combined, "10b6deacbc183772")
        self.assertIn("ci/validate_runtime_lock.py", setup)
        self.assertIn('if [ "$STACK_ROLE" = resolver ]; then', setup)
        self.assertIn('pip" install -r "$RUNTIME_LOCK_SEFARIA"', setup)
        self.assertIn('PYTHON_RUNTIME_ID="$CANONICAL_PYTHON_RUNTIME_ID"', setup)

    def test_gpu_producer_does_not_import_mongo_bound_sefaria_model(self):
        producer = (Path(__file__).parents[1] / "src/precompute_ner.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("from sefaria.model", producer)
        self.assertNotIn("django.setup", producer)
        self.assertIn("from sefaria.helper.normalization import NormalizerComposer", producer)

    def test_committed_fingerprint_is_accepted_lineage_and_builder_hashes_sources(self):
        root = Path(__file__).parents[1]
        engine_sources = (
            "src/link_books.py",
            "src/linker_artifact.py",
            "src/line_baseline.py",
            "src/incremental.py",
            "src/ner_handoff.py",
            "src/precompute_ner.py",
        )

        baseline = json.loads((root / "baseline/snapshot_hashes.json").read_text())
        metadata = json.loads((root / "meta.json").read_text())
        baseline_fingerprint = baseline["engine_fingerprint"]
        metadata_fingerprint = metadata["engine"]["fingerprint"]

        # These files describe the last ACCEPTED artifact release, not the dirty
        # working tree. Keeping its prior fingerprint is what makes a source edit
        # trigger compute_incremental_plan's mandatory full relink.
        self.assertEqual(baseline_fingerprint, metadata_fingerprint)
        setup = (root / "ci/setup_stack.sh").read_text(encoding="utf-8")
        for relative_path in engine_sources:
            self.assertIn(f'${{LINKER_REPO:-$PWD}}/{relative_path}', setup)


if __name__ == "__main__":
    unittest.main()
