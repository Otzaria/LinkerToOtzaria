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
        self.assertIn('path.parts[0] != "artifacts"', segment)
        self.assertIn('member.name == "meta.json" and member.isfile()', segment)
        self.assertIn("members=artifact_members", segment)
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

    def test_gpu_producer_does_not_import_mongo_bound_sefaria_model(self):
        producer = (Path(__file__).parents[1] / "src/precompute_ner.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("from sefaria.model", producer)
        self.assertNotIn("django.setup", producer)
        self.assertIn("from sefaria.helper.normalization import NormalizerComposer", producer)

    def test_committed_fingerprint_matches_split_engine_sources(self):
        root = Path(__file__).parents[1]
        digest = hashlib.sha256()
        for relative_path in (
            "src/link_books.py",
            "src/linker_artifact.py",
            "src/incremental.py",
            "src/ner_handoff.py",
            "src/precompute_ner.py",
        ):
            digest.update((root / relative_path).read_bytes())
        engine_component = f"engine_src={digest.hexdigest()[:16]}"

        baseline = json.loads((root / "baseline/snapshot_hashes.json").read_text())
        metadata = json.loads((root / "meta.json").read_text())
        baseline_fingerprint = baseline["engine_fingerprint"]
        metadata_fingerprint = metadata["engine"]["fingerprint"]

        self.assertEqual(baseline_fingerprint, metadata_fingerprint)
        self.assertIn(engine_component, baseline_fingerprint)


if __name__ == "__main__":
    unittest.main()
