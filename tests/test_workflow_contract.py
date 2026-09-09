import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
import unittest


# The two fingerprint components that a source change in THIS repo can move.
# Everything else in the fingerprint (dump, he_ref_ner, he_subref_ner, sefaria,
# gpu-server, python_runtime) comes from a pin or a fixed release asset and
# cannot move without a deliberate bump -- which must force a full relink, not
# be registered as an output-neutral migration.
ENGINE_SRC_FILES = (
    "src/link_books.py",
    "src/linker_artifact.py",
    "src/line_baseline.py",
    "src/incremental.py",
    "src/ner_handoff.py",
    "src/precompute_ner.py",
    "ci/gpu_server_microbatch.py",
    "ci/gpu_server_microbatch.patch",
)
SEFARIA_PATCH_FILE = "ci/sefaria_resolver.patch"


def yaml_mapping_scalar(text: str, *path: str) -> str:
    """Read one plain mapping scalar without adding a YAML dependency to CI.

    The workflow contract only needs a handful of single-line scalar values.  Keep
    that check aligned with this repository's zero-third-party-import test suite
    instead of making every PR install a YAML parser merely to inspect indentation.
    """
    stack = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^( *)([A-Za-z0-9_-]+):(?:[ \t]*(.*))?$", line)
        if match is None:
            continue
        indent, key, value = len(match.group(1)), match.group(2), match.group(3)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        current_path = tuple(item[1] for item in stack) + (key,)
        if current_path == path:
            if not value:
                raise AssertionError(f"{'.'.join(path)} is not a scalar")
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value
        if not value:
            stack.append((indent, key))
    raise KeyError(".".join(path))


def committed_blob(root: Path, relative_path: str) -> bytes:
    """The committed bytes of one file, as the runner hashes them.

    ci/setup_stack.sh runs on a Linux checkout and hashes LF bytes.  Reading the
    worktree instead is wrong on Windows, where .patch files are CRLF: it yields
    a fingerprint that no run can ever produce, and the guard below then compares
    two strings that are both fiction.
    """
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"HEAD:{relative_path}"],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"cannot read committed blob {relative_path!r}: "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result.stdout


def substitute_component(fingerprint: str, key: str, value: str) -> str:
    """Replace exactly one ``key=...`` component, anchored on the key."""
    parts = fingerprint.split(";")
    hits = [index for index, part in enumerate(parts) if part.startswith(f"{key}=")]
    if len(hits) != 1:
        raise KeyError(f"{key!r} appears {len(hits)} times in the fingerprint")
    parts[hits[0]] = f"{key}={value}"
    return ";".join(parts)


class RelinkWorkflowContractTest(unittest.TestCase):
    def test_relink_timeout_expressions_match_the_shared_contract(self):
        """Inspect required YAML scalars and bind shared timeouts to contract bytes.

        The 7,200-minute standalone ceiling is intentionally outside the
        cross-repository wait contract: no SeforimLibrary build waits for it.
        """
        root = Path(__file__).parents[1]
        contract_path = root / ".github/contracts/linker_relink_timeouts_v1.json"
        contract_bytes = contract_path.read_bytes()
        contract = json.loads(contract_bytes)
        self.assertEqual(
            hashlib.sha256(contract_bytes).hexdigest(),
            "a60c139ac604039d8a8af6a845cb818e96c56312e3327a17105459ec8f59c88f",
        )
        self.assertEqual(contract["contractVersion"], 1)
        self.assertEqual(contract["workflow"], "relink.yml")

        workflow = (root / ".github/workflows/relink.yml").read_text(encoding="utf-8")
        digest_path = ("on", "workflow_dispatch", "inputs", "wait_contract_sha256")
        self.assertEqual(yaml_mapping_scalar(workflow, *digest_path, "type"), "string")
        self.assertEqual(
            yaml_mapping_scalar(workflow, *digest_path, "default"),
            hashlib.sha256(contract_bytes).hexdigest(),
        )

        timeouts = contract["timeouts"]
        self.assertEqual(
            yaml_mapping_scalar(workflow, "jobs", "relink", "timeout-minutes"),
            "${{ inputs.target == 'kaggle' && "
            f"{timeouts['relink']['kaggle']} || "
            "(inputs.target == 'local' && "
            f"{timeouts['relink']['local']} || "
            "(inputs.library_run_id != '' && "
            f"{timeouts['relink']['server']} || 7200)) }}}}",
        )
        self.assertEqual(
            yaml_mapping_scalar(workflow, "jobs", "resolve", "timeout-minutes"),
            "${{ inputs.library_run_id != '' && "
            f"{timeouts['resolve']} || 7200 }}}}",
        )
        self.assertEqual(
            yaml_mapping_scalar(workflow, "jobs", "publish", "timeout-minutes"),
            str(timeouts["publish"]),
        )

        intake = (root / ".github/workflows/kaggle-relink.yml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(yaml_mapping_scalar(intake, *digest_path, "type"), "string")
        self.assertEqual(
            yaml_mapping_scalar(intake, *digest_path, "default"),
            hashlib.sha256(contract_bytes).hexdigest(),
        )

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
        self.assertIn("group: local-rocm-canary\n  cancel-in-progress: true", canary)
        self.assertIn('results = value.get("results")', canary)
        self.assertIn("not isinstance(results, list) or len(results) != 2", canary)
        self.assertIn('GPU_VENV_EXTERNAL="${LINKER_GPU_VENV:-}"', setup)
        self.assertIn('ACCELERATOR_PROFILE="${LINKER_ACCELERATOR_PROFILE:-}"', setup)
        self.assertIn("persistent GPU venv identity differs", setup)
        self.assertIn('ln -s "$GPU_VENV_EXTERNAL" "$GPU/.venv"', setup)
        self.assertIn('DUMP_ARCHIVE_DIR="$CACHE/dump-archives/$DUMP_CONTENT_ID"', setup)
        self.assertIn('tar -xzf "$DUMP_ARCHIVE"', setup)
        self.assertNotIn('tar -xzf "$CACHE/dump-dl/dump.tar.gz"', setup)
        self.assertIn('ci/gpu_server_microbatch.patch', setup)
        self.assertIn('git -C "$GPU" checkout -- app/app.py', setup)
        self.assertIn('git -C "$SEF" checkout --', setup)
        self.assertIn('sefaria/model/linker/referenceable_book_node.py', setup)
        self.assertIn('git -C "$SEF" apply --check "$PATCH"', setup)
        resolver_patch = (root / "ci/sefaria_resolver.patch").read_text(encoding="utf-8")
        self.assertIn("@lru_cache(maxsize=1024)", resolver_patch)
        self.assertIn("nodes.array()", resolver_patch)
        self.assertIn(
            'git -C "$GPU" apply --check --directory=app "$MICROBATCH_PATCH"', setup
        )
        self.assertIn('--worker-class gthread --threads "$NER_THREADS"', setup)
        self.assertIn('from otzaria_microbatch import OrderedMicroBatcher', setup)
        self.assertIn('"${LINKER_REPO:-$PWD}/ci/gpu_server_microbatch.py"', setup)
        self.assertIn("NER_THREADS: '16'", canary)
        self.assertIn("(inputs.target == 'local' && '2' ||", workflow)  # two GPU model processes
        self.assertIn("ci/ner_shared_model_probe.py", canary)

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

    def test_manual_mode_resolves_the_snapshot_release_from_db_provenance(self):
        # The SeforimLibrary DB release stopped publishing a second copy of
        # lines_snapshot.db.zst (build provenance schema_version 5 on): the bytes
        # only ever lived on the content-addressed pre-release the build published
        # before its own relink, and the DB release now NAMES that pre-release.
        # Standalone/manual mode must resolve it from build_provenance.json and
        # verify the download against the digest recorded there — fail-closed,
        # exactly as serial mode verifies the digest its parent pinned.
        root = Path(__file__).parents[1]
        workflow = (root / ".github/workflows/relink.yml").read_text(encoding="utf-8")
        fetch = (root / "ci/fetch_relink_inputs.sh").read_text(encoding="utf-8")

        for source in (workflow, fetch):
            self.assertIn("-p build_provenance.json -D inputs --clobber", source)
            self.assertIn(
                "PROV_SNAPSHOT_SHA=\"$(jq -r '.snapshot_zst_sha256 // \"\"' "
                'inputs/build_provenance.json)"',
                source,
            )
            self.assertIn(
                "PROV_SNAPSHOT_TAG=\"$(jq -r '.snapshot_release_tag // \"\"' "
                'inputs/build_provenance.json)"',
                source,
            )
            self.assertIn('[[ "$PROV_SNAPSHOT_SHA" =~ ^[0-9a-f]{64}$ ]]', source)
            # The tag is never trusted on its own: it must be the digest's own
            # content-addressed release, so a tampered or drifted provenance can
            # never point the relink at unrelated bytes.
            self.assertIn("lines-snapshot-sha256-$PROV_SNAPSHOT_SHA", source)
            self.assertIn(
                'gh release download "$PROV_SNAPSHOT_TAG" -R Otzaria/SeforimLibrary',
                source,
            )
            self.assertIn("sha256:$PROV_SNAPSHOT_SHA", source)
            # …and the bytes themselves are checked against that same digest.
            self.assertIn(
                'echo "$PROV_SNAPSHOT_SHA  inputs/lines_snapshot.db.zst" | sha256sum -c -',
                source,
            )
            # The legacy branch (releases up to v26, provenance schema_version <= 4,
            # which shipped the asset themselves) is a documented transitional path,
            # not a silent fallback: it must say when it can be removed.
            self.assertIn("from v27 on", source)

        # The workflow reads the DB release itself only for its provenance.
        self.assertIn(
            'gh release download "$LIB_TAG" -R Otzaria/SeforimLibrary \\\n'
            "              -p build_provenance.json -D inputs --clobber",
            workflow,
        )

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
        self.assertIn('artifact-store-releases/${remote_digest#sha256:}', segment)
        self.assertIn("reused verified local artifact-store payload", segment)
        self.assertIn('zstd -q -dc "$payload_source"', segment)
        self.assertNotIn("if: inputs.target != 'kaggle'", segment)
        self.assertNotIn('latest_tag="$(gh release view', segment)

        retain = workflow.split("- name: Retain verified local artifact-store payload", 1)[1]
        retain = retain.split("- name: Publish verified publisher handoff release", 1)[0]
        self.assertIn("inputs.target == 'local'", retain)
        self.assertIn('sha256sum linker_links.zst', retain)
        self.assertIn('artifact-store-releases/$payload_sha', retain)
        self.assertIn('mv -f "$cache_tmp" "$cache_payload"', retain)

    def test_attested_fingerprint_adoption_reaches_serial_recovery(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/relink.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('PLAN_ARGS+=(--adopt-fingerprint "$ADOPT_FINGERPRINT")', workflow)
        self.assertIn('ARGS+=(--adopt-fingerprint "$ADOPT_FINGERPRINT")', workflow)
        self.assertGreaterEqual(workflow.count("--forbid-full-relink"), 2)

    def test_full_relink_requires_explicit_exact_local_recovery(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/relink.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("allow_full_relink:", workflow)
        self.assertIn("ALLOW_FULL_RELINK: ${{ inputs.allow_full_relink && '1' || '' }}", workflow)
        self.assertIn('[ "$TARGET" = local ]', workflow)
        self.assertIn('.status == "completed" and .conclusion == "failure"', workflow)
        self.assertIn('allow_full_relink parent is not the exact failed Seforim build attempt', workflow)
        self.assertGreaterEqual(
            workflow.count('[ -n "$ALLOW_FULL_RELINK" ] || ARGS+=(--forbid-full-relink)'),
            1,
        )
        self.assertIn("local_checkpoint_source_run_id:", workflow)
        self.assertIn(
            'actions/runs/$LOCAL_CHECKPOINT_SOURCE_RUN_ID/attempts/$LOCAL_CHECKPOINT_SOURCE_RUN_ATTEMPT',
            workflow,
        )
        self.assertGreaterEqual(
            workflow.count('actions/runs/$LIBRARY_RUN_ID/attempts/$PARENT_RUN_ATTEMPT'),
            2,
        )
        self.assertIn("ci/local_checkpoint_cache.py restore", workflow)
        self.assertIn("ci/local_checkpoint_cache.py save", workflow)
        # The durable checkpoint is persisted periodically, not only by the EXIT trap
        # (which never runs when the kernel OOM-kills the runner).
        self.assertIn("LINKER_CHECKPOINT_SAVE_SECONDS", workflow)
        self.assertIn("save_local_checkpoint || echo \"::warning::periodic local checkpoint save failed", workflow)
        self.assertIn("kill \"$CHECKPOINT_SAVER_PID\"", workflow)
        self.assertIn("pkill -TERM -P \"$CHECKPOINT_SAVER_PID\"", workflow)
        self.assertIn('flock -w 1800 "$LINKER_LOCAL_CHECKPOINT_CACHE_ROOT/.save-$RELINK_REQUEST_ID.lock"', workflow)
        # target=local adopts its durable raw-NER bundle under an attested fingerprint.
        self.assertIn("adopting the durable local raw-NER bundle under attested engine fingerprint", workflow)
        self.assertIn('if [ -n "${NER_CHECKPOINT_SOURCE_ENGINE_FINGERPRINT:-}" ]; then', workflow)
        # The checkpoint-source guard lives in ci/ so every one of its conditions is
        # unit-tested (tests/test_local_checkpoint_source.py). The workflow must still
        # hand it the exact coordinates — above all THIS Linker commit.
        self.assertIn('python3 ci/validate_local_checkpoint_source.py "$source_json"', workflow)
        self.assertIn('--request-id "$RELINK_REQUEST_ID"', workflow)
        self.assertIn('--parent-run-attempt "$PARENT_RUN_ATTEMPT"', workflow)
        self.assertIn('--head-sha "$GITHUB_SHA"', workflow)
        guard = (
            Path(__file__).parents[1] / "ci/validate_local_checkpoint_source.py"
        ).read_text(encoding="utf-8")
        self.assertIn('TERMINAL_CONCLUSIONS = ("cancelled", "failure")', guard)
        self.assertIn('TITLE_PREFIXES = ("relink", "relink-recovery")', guard)
        self.assertIn('source.get("head_sha") != args.head_sha', guard)
        self.assertGreaterEqual(workflow.count('--repo "$PWD"'), 2)
        self.assertIn("--resume-checkpoints", workflow)
        self.assertIn("--engine-workers 12", workflow)
        self.assertIn("--engine-pool", workflow)
        self.assertIn("LINKER_RSS_CAP_BYTES=1200000000", workflow)
        self.assertIn("LINKER_HEAVY_BOOK_SLOTS=2", workflow)
        self.assertIn("LINKER_HEAVY_BOOK_GROWTH_BYTES=800000000", workflow)
        self.assertIn("LINKER_HEAVY_RSS_CAP_BYTES=5000000000", workflow)
        self.assertIn("LINKER_WORKER_ADDRESS_SPACE_BYTES=", workflow)
        self.assertIn('bash ci/stop_ner.sh', workflow)
        self.assertIn('--ner-bundle-dir "$NER_BUNDLE_DIR"', workflow)
        self.assertIn('NER_BUNDLE_DIR="$LINKER_LOCAL_CHECKPOINT_CACHE_ROOT/raw-ner/', workflow)
        self.assertIn("ci/collect_perf_telemetry.py", workflow)
        self.assertIn("export OMP_NUM_THREADS=1", workflow)
        self.assertIn("export OPENBLAS_NUM_THREADS=1", workflow)
        self.assertIn("ARGS+=(--engine-restart-limit 2)", workflow)
        # allow_full_relink authorizes a baseline MIGRATION; it no longer decides the
        # job ceiling — see test_parented_local_run_keeps_the_full_ceiling.
        self.assertNotIn("inputs.allow_full_relink && 1440", workflow)
        self.assertIn("LINKER_RESCAN_SECONDS: ${{ inputs.target == 'local' && '10' || '60' }}", workflow)
        self.assertIn("inputs.target == 'local' && '1'", workflow)
        self.assertIn("LINKER_NER_MICROBATCH_TEXTS", workflow)
        self.assertIn("inputs.target == 'local' && '150'", workflow)

    def test_kaggle_is_ner_only_and_resolution_runs_on_server(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/relink.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("export LINKER_STACK_ROLE=ner", workflow)
        self.assertIn("src/precompute_ner.py", workflow)
        self.assertIn("NER_PRODUCER_WORKERS=2", workflow)
        self.assertIn("name: Resolve raw NER on the durable CPU host", workflow)
        self.assertIn("runs-on: [self-hosted, Linux, ARM64, server-2]", workflow)
        self.assertIn("LINKER_STACK_ROLE: resolver", workflow)
        self.assertIn("--ner-bundle-dir", workflow)
        self.assertIn("--engine-workers 2", workflow)
        self.assertIn("LINKER_BATCH_LINES: 25", workflow)
        self.assertIn("LINKER_BATCH_CHARS: 60000", workflow)
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

    def test_parented_local_run_keeps_the_full_ceiling(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/relink.yml").read_text(
            encoding="utf-8"
        )
        # A serial (parented) local run must NOT inherit the 480-minute CPU-resolve
        # ceiling of the split topology: on local one job pays GPU NER *and* resolve.
        # Run 33994031370 packed a complete payload 12 s after being cancelled at 480 min.
        self.assertIn(
            "timeout-minutes: ${{ inputs.target == 'kaggle' && 90 || "
            "(inputs.target == 'local' && 1440 || "
            "(inputs.library_run_id != '' && 480 || 7200)) }}",
            workflow,
        )
        # The CPU-resolve job never carries the GPU stage, so it keeps 480/7200.
        self.assertIn(
            "timeout-minutes: ${{ inputs.library_run_id != '' && 480 || 7200 }}",
            workflow,
        )

    def test_unpublished_payload_survives_the_always_cleanup(self):
        root = Path(__file__).parents[1]
        workflow = (root / ".github/workflows/relink.yml").read_text(encoding="utf-8")
        packer = (root / "ci/pack_and_publish.sh").read_text(encoding="utf-8")

        # The payload leaves the run-scoped workspace BEFORE it is announced, so a
        # cancel between packing and publishing cannot take it with it.
        self.assertIn(
            'PRESERVED="${LINKER_CACHE_DIR:-$HOME/.cache/linker-stack}'
            "/unpublished-payloads/$SHA\"",
            packer,
        )
        self.assertIn('mv -f "$PRESERVED_TMP" "$PRESERVED/$OUT"', packer)
        # A workspace fallback would be git-cleaned by the next run's checkout.
        self.assertNotIn("{LINKER_CACHE_DIR:-$PWD}", packer)
        self.assertNotIn("{LINKER_CACHE_DIR:-$PWD}", workflow)
        # Exactly one line about the packed payload -- not a firehose.
        self.assertEqual(
            packer.count('echo "payload packed: $PRESERVED/$OUT sha256=$SHA (preserved for recovery)"'),
            1,
        )
        self.assertEqual(packer.count("payload packed:"), 1)

        # Both always() cleanups drop that copy only once the handoff really shipped
        # the bytes; otherwise they keep it and say where it is.
        self.assertEqual(workflow.count("id: publish_handoff"), 2)
        self.assertEqual(
            workflow.count("HANDOFF_PUBLISHED: ${{ steps.publish_handoff.outcome == 'success' }}"),
            2,
        )
        self.assertEqual(workflow.count('if [ "$HANDOFF_PUBLISHED" = true ]; then'), 2)
        self.assertEqual(workflow.count('rm -rf "$preserved"'), 2)
        self.assertEqual(workflow.count("unpublished payload retained for recovery:"), 2)

        # Nothing adopts the preserved directory automatically: the artifact store is
        # still restored from a PUBLISHED release, so a payload kept here can never be
        # picked up by a run with a different engine fingerprint.
        self.assertEqual(workflow.count("unpublished-payloads"), 2)
        for relative in ("ci/restore_artifact_store.sh", "src/incremental.py"):
            with self.subTest(consumer=relative):
                self.assertNotIn(
                    "unpublished-payloads",
                    (root / relative).read_text(encoding="utf-8"),
                )

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
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("linker-perf-${{ github.run_id }}", workflow)

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

    def resolve_migration(self, root: Path, actual: str) -> str:
        """Run the real resolver the workflow runs, against the real registry."""
        result = subprocess.run(
            [
                sys.executable,
                str(root / "ci/resolve_output_neutral_fingerprint_migration.py"),
                "--baseline",
                str(root / "baseline/snapshot_hashes.json"),
                "--actual",
                actual,
                "--migrations",
                str(root / "baseline/output_neutral_fingerprint_migrations.json"),
            ],
            capture_output=True,
            text=True,
            cwd=str(root),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_committed_fingerprint_is_accepted_lineage_not_dirty_source(self):
        """HEAD's engine must be adoptable from the committed lineage.

        What this proves, exactly: ``engine_src`` and ``sefaria_patch`` recomputed
        from HEAD's blobs -- the only two fingerprint components a source change in
        this repo can move -- substituted into the committed lineage fingerprint,
        are resolved by ci/resolve_output_neutral_fingerprint_migration.py into the
        exact ``OLD::NEW`` adoption contract, using the same baseline file and the
        same registry the workflow passes it.  So the next relink at this commit
        adopts instead of relinking all ~7,300 books.  Running the resolver rather
        than re-implementing the lookup also re-asserts the registry's schema and
        its per-entry ``review`` requirement, and that no two entries are ambiguous.

        And that the guard is EXACT: a one-character drift in either component is
        not resolved, so an engine that is not the reviewed one still forces a full
        relink.

        What this does NOT prove: that the change is output-neutral.  Only the
        review text carries that claim; this asserts a reviewed entry exists and
        matches byte-for-byte.  It also assumes the pinned components (dump,
        models, sefaria, gpu-server, python_runtime) are unchanged -- a pin bump
        must force a full relink and must never be registered here.

        This assertion was inverted by 1877071 for a semantic change and never
        restored; it is a POSITIVE guard, and the empty-substitution branch below
        is the only case in which it asserts nothing.
        """
        root = Path(__file__).parents[1]
        if not (root / ".git").exists():
            self.skipTest("not a git checkout: committed blobs are unavailable")

        digest = hashlib.sha256()
        for relative_path in ENGINE_SRC_FILES:
            digest.update(committed_blob(root, relative_path))
        engine_src = digest.hexdigest()[:16]
        sefaria_patch = hashlib.sha256(
            committed_blob(root, SEFARIA_PATCH_FILE)
        ).hexdigest()[:16]

        baseline = json.loads((root / "baseline/snapshot_hashes.json").read_text())
        metadata = json.loads((root / "meta.json").read_text())
        baseline_fingerprint = baseline["engine_fingerprint"]
        metadata_fingerprint = metadata["engine"]["fingerprint"]

        self.assertEqual(baseline_fingerprint, metadata_fingerprint)

        current_fingerprint = substitute_component(
            substitute_component(baseline_fingerprint, "engine_src", engine_src),
            "sefaria_patch",
            sefaria_patch,
        )
        if current_fingerprint == baseline_fingerprint:
            # The committed engine IS the published lineage; nothing to migrate.
            self.assertEqual(self.resolve_migration(root, current_fingerprint), "")
            return

        migrations = json.loads(
            (root / "baseline/output_neutral_fingerprint_migrations.json").read_text()
        )
        self.assertEqual(migrations.get("schema_version"), 1)
        self.assertEqual(
            self.resolve_migration(root, current_fingerprint),
            f"{baseline_fingerprint}::{current_fingerprint}",
            "the engine committed here has drifted from the published lineage without "
            "a reviewed output-neutral migration entry: the next relink would rebuild "
            "every book",
        )

        for key, value in (("engine_src", engine_src), ("sefaria_patch", sefaria_patch)):
            mutated_value = value[:-1] + ("0" if value[-1] != "0" else "1")
            mutated = substitute_component(current_fingerprint, key, mutated_value)
            with self.subTest(mutated=key):
                self.assertEqual(
                    self.resolve_migration(root, mutated),
                    "",
                    f"a one-character drift in {key} must NOT be adopted",
                )

    def test_pack_steps_saturate_the_runner_without_losing_determinism(self):
        root = Path(__file__).parents[1]
        policy = (root / "ci/zstd_mt.sh").read_text(encoding="utf-8")

        # The worker count must come from the LOGICAL CPUs. `zstd -T0` resolves
        # to physical cores, which is half of them on the WSL2 runner.
        self.assertIn("nproc", policy)
        self.assertIn("zstd_workers()", policy)
        # Bounded so a very wide host cannot blow the memory budget, and a
        # missing nproc degrades to zstd's own detection rather than failing.
        self.assertIn("n=32", policy)
        self.assertIn("n=0", policy)

        packers = {
            "ci/pack_and_publish.sh": "-19",
            "ci/pack_ner_handoff.sh": "-12",
            "ci/pack_ner_checkpoint.sh": "-8",
        }
        for relative, level in packers.items():
            script = (root / relative).read_text(encoding="utf-8")
            with self.subTest(script=relative):
                self.assertIn("zstd_mt.sh", script)
                # No pack step may go back to physical-core detection.
                self.assertNotIn(f"zstd {level} -T0", script)
                self.assertNotIn("-T0 -o", script)

        payload = (root / "ci/pack_and_publish.sh").read_text(encoding="utf-8")
        # Job size and overlap are pinned, so the payload bytes stop depending on
        # zstd's version-specific multi-threading defaults.
        self.assertIn("ZSTD_TUNING=(-19 -B4M --zstd=ovlog=6)", payload)
        # Unsupported tuning must fail the step, never silently fall back to
        # different settings -- that would make the payload sha host-dependent.
        self.assertIn("refusing to pack with different settings", payload)
        # The container is unchanged: still one plain zstd frame over the tar.
        self.assertNotIn("--long", payload)
        self.assertIn("-cf - artifacts line-baseline meta.json", payload)


if __name__ == "__main__":
    unittest.main()
