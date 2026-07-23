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
        segment = workflow.split('ARGS=(', 1)[1].split(
            '"$SEF_PROJECT/.venv/bin/python" src/incremental.py', 1
        )[0]
        serial_branch, after_branch = segment.split(
            "          # An explicit OLD::NEW attestation", 1
        )
        self.assertIn("--forbid-full-relink", serial_branch)
        self.assertNotIn("--adopt-fingerprint", serial_branch)
        self.assertIn('ARGS+=(--adopt-fingerprint "$ADOPT_FINGERPRINT")', after_branch)

    def test_serial_kaggle_relink_admits_bounded_workers_by_actual_memory(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/relink.yml").read_text(
            encoding="utf-8"
        )
        args_segment = workflow.split("          ARGS=(", 1)[1].split(
            '"$SEF_PROJECT/.venv/bin/python" src/incremental.py', 1
        )[0]
        segment = args_segment.split('if [ -n "$LIBRARY_RUN_ID" ]; then', 1)[1].split(
            "          # An explicit OLD::NEW attestation", 1
        )[0]
        self.assertIn('case "$TARGET" in', segment)
        self.assertIn("MEM_TOTAL_KIB=", segment)
        self.assertIn('[ "$MEM_TOTAL_KIB" -ge 24000000 ]', segment)
        self.assertIn("ENGINE_WORKERS=2", segment)
        self.assertIn("ENGINE_WORKERS=1", segment)
        self.assertIn('ARGS+=(--engine-workers "$ENGINE_WORKERS")', segment)
        self.assertIn("*) ARGS+=(--engine-workers 2) ;;", segment)
        self.assertIn("ARGS+=(--forbid-full-relink)", segment)
        self.assertIn("LINKER_BATCH_LINES: ${{ inputs.target == 'kaggle' && '25' || '100' }}", workflow)
        self.assertIn("inputs.library_run_id != '' && '1' || '2'", workflow)

    def test_serial_kaggle_timeout_fits_ephemeral_session(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/relink.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("inputs.target == 'kaggle' && 525 || 480", workflow)
        self.assertIn("below the ephemeral session's ~9h lifetime", workflow)


if __name__ == "__main__":
    unittest.main()
