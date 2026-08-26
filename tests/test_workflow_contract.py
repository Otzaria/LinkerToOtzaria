import hashlib
import json
from pathlib import Path
import unittest


class RelinkWorkflowContractTest(unittest.TestCase):
    def test_local_rocm_target_uses_dedicated_runner_and_persistent_venv(self):
        root = Path(__file__).parents[1]
        workflow = (root / ".github/workflows/relink.yml").read_text(encoding="utf-8")
        setup = (root / "ci/setup_stack.sh").read_text(encoding="utf-8")

        self.assertIn("options: [local, server, kaggle]", workflow)
        self.assertIn("default: 'local'", workflow)
        self.assertIn("otzaria-linker", workflow)
        self.assertIn("amd-gpu", workflow)
        self.assertIn("LINKER_GPU_VENV:", workflow)
        self.assertIn("LINKER_ACCELERATOR_PROFILE:", workflow)
        self.assertIn("LINKER_CACHE_DIR:", workflow)
        self.assertIn("HSA_ENABLE_DXG_DETECTION:", workflow)
        canary = (root / ".github/workflows/local-runner-canary.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Verify durable host toolchain", canary)
        self.assertIn("sudo -n true", canary)
        self.assertIn('results = value.get("results")', canary)
        self.assertIn("not isinstance(results, list) or len(results) != 2", canary)
        self.assertIn('GPU_VENV_EXTERNAL="${LINKER_GPU_VENV:-}"', setup)
        self.assertIn('ACCELERATOR_PROFILE="${LINKER_ACCELERATOR_PROFILE:-}"', setup)
        self.assertIn("persistent GPU venv identity differs", setup)
        self.assertIn('ln -s "$GPU_VENV_EXTERNAL" "$GPU/.venv"', setup)
        self.assertIn('DUMP_ARCHIVE_DIR="$CACHE/dump-archives/$DUMP_CONTENT_ID"', setup)
        self.assertIn('tar -xzf "$DUMP_ARCHIVE"', setup)
        self.assertNotIn('tar -xzf "$CACHE/dump-dl/dump.tar.gz"', setup)

    def test_recovery_guards_are_event_driven_and_exact(self):
        root = Path(__file__).parents[1]
        provision = (root / ".github/workflows/kaggle-provisioner.yml").read_text(
            encoding="utf-8"
        )
        rerun = (root / ".github/workflows/kaggle-rerun-provisioner.yml").read_text(
            encoding="utf-8"
        )
        reconcile = (root / ".github/workflows/reconcile-pipeline.yml").read_text(
            encoding="utf-8"
        )
        intake = (root / ".github/workflows/kaggle-relink.yml").read_text(
            encoding="utf-8"
        )

        for workflow in (provision, rerun, reconcile):
            header = workflow.split("jobs:\n", 1)[0]
            self.assertNotIn("schedule:", header)
            self.assertNotIn("cron:", header)
            self.assertIn("workflow_dispatch:", header)
        self.assertIn("intent_run_id:\n        description:", provision)
        self.assertIn("required: true", provision)
        self.assertIn('provision_kaggle_rerun.sh --run-id "$RUN_ID"', rerun)
        self.assertIn("required: true", rerun)
        wake = intake.split("- name: Wake singleton provisioner", 1)[1]
        self.assertIn("exact provisioner could not be started", wake)
        self.assertIn("exit 1", wake)
        self.assertNotIn("scheduled tick", wake)

    def test_release_publisher_rejects_asset_names_github_would_normalize(self):
        helper = (
            Path(__file__).parents[1] / "ci/publish_release_handoff.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("release asset basename is unsafe or would be normalized by GitHub", helper)
        self.assertIn("^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$", helper)
        self.assertIn('repos/$GITHUB_REPOSITORY/releases/tags/$tag', helper)
        self.assertIn("targetCommitish:.target_commitish", helper)
        self.assertNotIn('gh release view "$tag" --json', helper)

    def test_serial_snapshot_handoff_uses_content_addressed_release(self):
        root = Path(__file__).parents[1]
        workflow = (root / ".github/workflows/relink.yml").read_text(encoding="utf-8")
        fetch = (root / "ci/fetch_relink_inputs.sh").read_text(encoding="utf-8")
        provision = (root / "scripts/provision_kaggle_intent.sh").read_text(encoding="utf-8")

        for source in (workflow, fetch):
            self.assertIn('lines-snapshot-sha256-$SNAPSHOT_SHA256', source)
            self.assertIn('gh release download "$SNAPSHOT_TAG"', source)
            self.assertIn('sha256:$SNAPSHOT_SHA256', source)
            self.assertNotIn('gh run download "$LIBRARY_RUN_ID"', source)
        self.assertIn('snapshot_tag="lines-snapshot-sha256-$snapshot_sha256"', provision)
        self.assertIn("recovery snapshot release is missing or not byte-exact", provision)
        self.assertNotIn("recovery snapshot artifact", provision)
        self.assertIn(
            '"repos/Otzaria/SeforimLibrary/releases/tags/$SNAPSHOT_TAG"', workflow
        )
        self.assertIn(
            '"repos/Otzaria/SeforimLibrary/releases/tags/$LIB_TAG"', workflow
        )
        self.assertNotIn('REMOTE_SNAPSHOT_DIGEST="$(gh release view', workflow)

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
        self.assertIn("Publish content-addressed raw-NER handoff release", workflow)
        self.assertIn("ci/publish_release_handoff.sh", workflow)
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

    def test_resolver_only_recovery_uses_exact_producer_release(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/relink.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("raw_ner_source_run_id:", workflow)
        self.assertIn("raw_ner_source_run_attempt:", workflow)
        self.assertIn("Validate exact raw-NER recovery source", workflow)
        self.assertIn('expected_recovery_title="relink-recovery request=', workflow)
        self.assertIn(".display_title == $title or .display_title == $recovery_title", workflow)
        self.assertIn("raw-ner-handoff-{0}-{1}", workflow)
        self.assertIn("needs.relink.outputs.raw_ner_release_tag", workflow)
        self.assertIn('gh release download "$RELEASE_TAG"', workflow)
        self.assertIn(
            'repos/$GITHUB_REPOSITORY/compare/${SOURCE_HEAD}...${GITHUB_SHA}',
            workflow,
        )
        self.assertIn(".merge_base_commit.sha == $source", workflow)
        self.assertIn("raw-NER recovery release identity/assets differ", workflow)
        self.assertNotIn("actions/download-artifact", workflow)
        self.assertNotIn("actions/upload-artifact", workflow)

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

    def test_committed_fingerprint_matches_split_engine_sources(self):
        root = Path(__file__).parents[1]
        digest = hashlib.sha256()
        for relative_path in (
            "src/link_books.py",
            "src/linker_artifact.py",
            "src/line_baseline.py",
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
        if engine_component not in baseline_fingerprint:
            migrations = json.loads(
                (root / "baseline/output_neutral_fingerprint_migrations.json").read_text()
            )
            current_fingerprint = baseline_fingerprint.replace(
                baseline_fingerprint.split("engine_src=")[1].split(";", 1)[0],
                engine_component.split("=", 1)[1],
            )
            self.assertEqual(migrations.get("schema_version"), 1)
            self.assertTrue(
                any(
                    entry.get("from") == baseline_fingerprint
                    and entry.get("to") == current_fingerprint
                    and entry.get("review")
                    for entry in migrations.get("migrations", [])
                )
            )


if __name__ == "__main__":
    unittest.main()
