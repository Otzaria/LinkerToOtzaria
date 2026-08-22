#!/usr/bin/env bash
# Dispatch a relink onto a fresh Kaggle GPU session.
#
#   scripts/dispatch_kaggle_relink.sh [--dry-run] [--recovery-mode] [--library-run-id <id>] \
#       [--relink-request-id <64-hex>] [--parent-run-attempt <n>] \
#       [--sefaria-tag <tag>] [--snapshot-sha256 <sha>] \
#       [--sefaria-release-metadata-sha256 <sha>] [--adopt-fingerprint <OLD::NEW>] \
#       [--ner-checkpoint-source-run-id <id>] \
#       [--ner-checkpoint-source-run-attempt <n>] \
#       [--ner-checkpoint-source-engine-fingerprint <fingerprint>]
#
# --library-run-id: serial mode — the relink takes lines_snapshot.db.zst from that
# SeforimLibrary run and the waiting build downloads the artifacts back from it.
# --relink-request-id / --parent-run-attempt: the build's per-attempt correlation
# identity — stamped into the child's run-name for EXACT-match discovery/cleanup.
# --sefaria-tag / --snapshot-sha256 / --sefaria-release-metadata-sha256: the build's
# pinned Sefaria vintage + snapshot + metadata digests, forwarded to relink.yml.
#
# Order matters: the relink job is queued FIRST (runs-on [self-hosted, kaggle, gpu] —
# it just waits), THEN the kernel is pushed; the session boots, registers as a one-job
# JIT runner and picks the job up. Requires: gh authenticated with repo-admin rights,
# kaggle CLI authenticated (both true on the operator machine and in kaggle-relink.yml),
# nothing else — the kernel is self-contained (bootstrap.sh + JIT config embedded into
# run.py) and pulls its pinned tool binaries from the output of the one-shot
# otzaria/linker-tools-fetcher kernel (attached via kernel_sources).
set -euo pipefail
REPO=Otzaria/LinkerToOtzaria
KERNEL_ID=otzaria/linker-gpu-runner
TOOLS_KERNEL=otzaria/linker-tools-fetcher
RUNTIME_KERNEL=otzaria/linker-python-runtime
HERE=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=lib/gh_runs.sh
source "$HERE/scripts/lib/gh_runs.sh"

DRY=""
RECOVERY_MODE=""
LIBRARY_RUN_ID=""
SEFARIA_TAG=""
SNAPSHOT_SHA256=""
SEFARIA_METADATA_SHA256=""
RELINK_REQUEST_ID=""
PARENT_RUN_ATTEMPT=""
ADOPT_FINGERPRINT=""
NER_CHECKPOINT_SOURCE_RUN_ID=""
NER_CHECKPOINT_SOURCE_RUN_ATTEMPT=""
NER_CHECKPOINT_SOURCE_ENGINE_FINGERPRINT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --recovery-mode) RECOVERY_MODE=1; shift ;;
    --library-run-id) LIBRARY_RUN_ID="$2"; shift 2 ;;
    --relink-request-id) RELINK_REQUEST_ID="$2"; shift 2 ;;
    --parent-run-attempt) PARENT_RUN_ATTEMPT="$2"; shift 2 ;;
    --sefaria-tag) SEFARIA_TAG="$2"; shift 2 ;;
    --snapshot-sha256) SNAPSHOT_SHA256="$2"; shift 2 ;;
    --sefaria-release-metadata-sha256) SEFARIA_METADATA_SHA256="$2"; shift 2 ;;
    --adopt-fingerprint) ADOPT_FINGERPRINT="$2"; shift 2 ;;
    --ner-checkpoint-source-run-id) NER_CHECKPOINT_SOURCE_RUN_ID="$2"; shift 2 ;;
    --ner-checkpoint-source-run-attempt) NER_CHECKPOINT_SOURCE_RUN_ATTEMPT="$2"; shift 2 ;;
    --ner-checkpoint-source-engine-fingerprint)
      NER_CHECKPOINT_SOURCE_ENGINE_FINGERPRINT="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Validate EVERYTHING that will be embedded in dispatch arguments — this script may
# be fed workflow_dispatch inputs; nothing unvalidated reaches a command line.
[ -z "$LIBRARY_RUN_ID" ] || [[ "$LIBRARY_RUN_ID" =~ ^[1-9][0-9]*$ ]] || { echo "--library-run-id must be a positive integer" >&2; exit 2; }
[ -z "$SEFARIA_TAG" ] || [[ "$SEFARIA_TAG" =~ ^[A-Za-z0-9._-]{1,100}$ ]] || { echo "--sefaria-tag has an invalid shape" >&2; exit 2; }
[ -z "$SNAPSHOT_SHA256" ] || [[ "$SNAPSHOT_SHA256" =~ ^[0-9a-f]{64}$ ]] || { echo "--snapshot-sha256 must be 64-hex" >&2; exit 2; }
[ -z "$SEFARIA_METADATA_SHA256" ] || [[ "$SEFARIA_METADATA_SHA256" =~ ^[0-9a-f]{64}$ ]] || { echo "--sefaria-release-metadata-sha256 must be 64-hex" >&2; exit 2; }
[ -z "$ADOPT_FINGERPRINT" ] || \
  python3 "$HERE/ci/validate_printable_ascii.py" adopt_fingerprint 8192 "$ADOPT_FINGERPRINT"
[ -z "$NER_CHECKPOINT_SOURCE_RUN_ID" ] || \
  [[ "$NER_CHECKPOINT_SOURCE_RUN_ID" =~ ^[1-9][0-9]*$ ]] || {
    echo "--ner-checkpoint-source-run-id must be a positive integer" >&2; exit 2;
  }
[ -z "$NER_CHECKPOINT_SOURCE_RUN_ATTEMPT" ] || \
  [[ "$NER_CHECKPOINT_SOURCE_RUN_ATTEMPT" =~ ^[1-9][0-9]*$ ]] || {
    echo "--ner-checkpoint-source-run-attempt must be a positive integer" >&2; exit 2;
  }
[ -z "$NER_CHECKPOINT_SOURCE_ENGINE_FINGERPRINT" ] || \
  python3 "$HERE/ci/validate_printable_ascii.py" \
    ner_checkpoint_source_engine_fingerprint 8192 \
    "$NER_CHECKPOINT_SOURCE_ENGINE_FINGERPRINT"
if [ -n "$NER_CHECKPOINT_SOURCE_RUN_ID" ] ||
   [ -n "$NER_CHECKPOINT_SOURCE_RUN_ATTEMPT" ] ||
   [ -n "$NER_CHECKPOINT_SOURCE_ENGINE_FINGERPRINT" ]; then
  [ -n "$NER_CHECKPOINT_SOURCE_RUN_ID" ] &&
    [ -n "$NER_CHECKPOINT_SOURCE_RUN_ATTEMPT" ] &&
    [ -n "$NER_CHECKPOINT_SOURCE_ENGINE_FINGERPRINT" ] || {
    echo "checkpoint recovery requires both source coordinates and its engine fingerprint" >&2
    exit 2
  }
  [ -n "$RECOVERY_MODE" ] && [ -n "$LIBRARY_RUN_ID" ] || {
    echo "checkpoint recovery requires --recovery-mode and an exact serial parent" >&2
    exit 2
  }
fi

# Identity contract: EVERY dispatch carries a request id so the child is addressable
# by exact run-name equality (never contains / head -1). Serial mode must receive the
# build's deterministic per-attempt id; standalone derives one from the dispatcher
# run's own coordinates (CI) or fresh randomness (operator machine).
if [ -n "$LIBRARY_RUN_ID" ]; then
  [[ "$RELINK_REQUEST_ID" =~ ^[0-9a-f]{64}$ ]] || { echo "serial dispatch requires --relink-request-id (64-hex sha256)" >&2; exit 2; }
  [[ "$PARENT_RUN_ATTEMPT" =~ ^[1-9][0-9]*$ ]] || { echo "serial dispatch requires --parent-run-attempt (positive integer)" >&2; exit 2; }
fi
[ -z "$RECOVERY_MODE" ] || [ -n "$LIBRARY_RUN_ID" ] || { echo "--recovery-mode requires an exact serial parent" >&2; exit 2; }
if [ -z "$RELINK_REQUEST_ID" ]; then
  if [ -n "${GITHUB_RUN_ID:-}" ]; then
    RELINK_REQUEST_ID=$(printf 'relink-request-v1 standalone %s %s %s' \
      "${GITHUB_REPOSITORY:-}" "$GITHUB_RUN_ID" "${GITHUB_RUN_ATTEMPT:-1}" | sha256sum | cut -d' ' -f1)
  else
    RELINK_REQUEST_ID=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
  fi
fi
[[ "$RELINK_REQUEST_ID" =~ ^[0-9a-f]{64}$ ]] || { echo "--relink-request-id must be 64-hex" >&2; exit 2; }
JIT_REQUEST_LABEL="request-$RELINK_REQUEST_ID"
# Must byte-match relink.yml's run-name template — the cleanup trap and the waiting
# build both compare the FULL title with ==.
if [ -n "$LIBRARY_RUN_ID" ]; then
  CHILD_PREFIX=relink
  [ -z "$RECOVERY_MODE" ] || CHILD_PREFIX=relink-recovery
  CHILD_TITLE="$CHILD_PREFIX request=$RELINK_REQUEST_ID parent=$LIBRARY_RUN_ID:$PARENT_RUN_ATTEMPT"
else
  CHILD_TITLE="relink request=$RELINK_REQUEST_ID parent=standalone"
fi

# Admission gate — BEFORE provisioning anything. A Kaggle session lives ~9h; booting
# one while another relink is active/pending would leave the new child queued while
# its JIT session idles, burning the GPU session. Refuse loudly instead. In CI the
# provisioner job holds the SAME `linker-relink` concurrency mutex as every real
# child from this check through JIT creation, dispatch and kernel push.  The child
# queues behind its own provisioner and can start only after the JIT is ready.
# (An operator-machine run has no mutex; the check still narrows the window, and the
# linker-relink group + reconciler bound the damage.) Fully paginated + fail-closed:
# a broken listing refuses to provision blind.
if ! BUSY=$(count_runs_active "$REPO" relink.yml); then
  echo "::error::admission listing failed — refusing to provision blind" >&2
  exit 1
fi
if [ "$BUSY" -ne 0 ]; then
  echo "::error::$BUSY relink run(s) still active/pending — refusing to provision a Kaggle session that would idle behind the publisher slot. Re-dispatch when the linker is free." >&2
  exit 1
fi

# A previous ephemeral listener can remain registered briefly after its one job
# becomes terminal. Reap only idle linker-owned JIT registrations before creating
# the next one; an unexpectedly busy registration is an invariant failure. The
# request-specific label below is the hard isolation boundary even if an old
# kernel registers in the tiny interval after this sweep.
if ! STALE_RUNNERS=$(gh api --paginate -X GET "repos/$REPO/actions/runners" -f per_page=100 \
    --jq '.runners[] | select(.name|startswith("kaggle-")) | [.id,.name,.busy,.status] | @tsv'); then
  echo "::error::cannot enumerate stale Kaggle JIT registrations" >&2
  exit 1
fi
while IFS=$'\t' read -r stale_id stale_name stale_busy stale_status; do
  [ -n "$stale_id" ] || continue
  [[ "$stale_id" =~ ^[1-9][0-9]*$ ]] || { echo "::error::invalid stale runner id" >&2; exit 1; }
  if [ "$stale_busy" = true ]; then
    echo "::error::stale JIT runner $stale_name ($stale_id) is busy while no relink run is active" >&2
    exit 1
  fi
  [ "$stale_busy" = false ] || { echo "::error::invalid busy state for runner $stale_name" >&2; exit 1; }
  echo "removing idle stale JIT registration: $stale_name ($stale_status)"
  gh api -X DELETE "repos/$REPO/actions/runners/$stale_id"
done <<< "$STALE_RUNNERS"

NAME="kaggle-${RELINK_REQUEST_ID:0:16}-$(date +%Y%m%d-%H%M%S)"
JIT=$(gh api -X POST "repos/$REPO/actions/runners/generate-jitconfig" \
  -f name="$NAME" -F runner_group_id=1 -f work_folder=_work \
  -f 'labels[]=self-hosted' -f 'labels[]=kaggle' -f 'labels[]=gpu' -f "labels[]=$JIT_REQUEST_LABEL" \
  -q .encoded_jit_config)
echo "JIT runner registered: $NAME"

# ── Cleanup contract ────────────────────────────────────────────────────────────
# The child is queued BEFORE the kernel push; without a booted session it would sit
# queued until its own timeout. The traps are armed BEFORE the dispatch call: a
# response-lost dispatch (5xx answer, run actually created) is still reaped by the
# exact-title search. From here until the push succeeds, EVERY exit path — a failed
# dispatch/mktemp/base64/heredoc/push, SIGINT/SIGTERM (job cancel/timeout), any early
# exit — must cancel OUR child and only ours: exact title equality on the request id.
# Handlers disable all traps on entry (a signal followed by EXIT must not clean up
# twice) and record the proper conclusion (130/143 for signals, the real code on exit).
CHILD_RUN_ID=""
CLEANUP_DONE=0
DISPATCH_ATTEMPTED=0
ACTIVE_PID=""
terminate_active() {
  local pid="${ACTIVE_PID:-}"
  [ -n "$pid" ] || return 0
  kill -TERM -- "-$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
  kill -KILL -- "-$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  ACTIVE_PID=""
}
DISPATCHED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

find_child_live() {  # live (pre-terminal) runs matching OUR exact title; fail loud
  local rows
  rows=$(list_runs_active "$REPO" relink.yml) || return 1
  printf '%s\n' "$rows" | awk -F'\t' -v t="$CHILD_TITLE" '$3 == t {print $1}'
}

cancel_child() {
  [ "$CLEANUP_DONE" = 1 ] && return 0
  CLEANUP_DONE=1
  [ "$DISPATCH_ATTEMPTED" = 1 ] || return 0  # nothing was ever queued
  echo "dispatch is exiting before the kernel was pushed — cancelling the queued relink ($CHILD_TITLE)" >&2
  local ids ok attempt rid query_ok=1
  for attempt in 1 2 3; do
    if [ -n "$CHILD_RUN_ID" ]; then
      ids="$CHILD_RUN_ID"
    elif ! ids="$(find_child_live)"; then
      query_ok=0; ids=""
    fi
    if [ -n "$ids" ]; then
      ok=1
      for rid in $ids; do gh run cancel "$rid" -R "$REPO" || ok=0; done
      [ "$ok" = 1 ] && { echo "cancelled: $ids" >&2; return 0; }
    fi
    sleep 5  # API eventual consistency — the fresh run may not be listed yet
  done
  # Never flip the original failure into success. Wake the event-driven reconciler
  # immediately; the failed parent completion will wake it once more if this
  # dispatcher dies before the child becomes visible.
  local wake_attempt
  for wake_attempt in 1 2 3; do
    if gh workflow run reconcile-pipeline.yml -R "$REPO"; then
      echo "::warning::ORPHAN_INTENT relink_request_id=$RELINK_REQUEST_ID child_run_id=${CHILD_RUN_ID:-unknown} query_ok=$query_ok cancel failed — event-driven reconciliation dispatched" >&2
      return 0
    fi
    [ "$wake_attempt" = 3 ] || sleep "$((wake_attempt * 5))"
  done
  echo "::warning::ORPHAN_INTENT relink_request_id=$RELINK_REQUEST_ID child_run_id=${CHILD_RUN_ID:-unknown} query_ok=$query_ok cancel failed and reconciliation dispatch failed" >&2
  return 0
}
on_exit()   { local ec=$?; trap - EXIT HUP INT TERM; if [ "$ec" -ne 0 ]; then terminate_active; cancel_child; fi; exit "$ec"; }
on_signal() { trap - EXIT HUP INT TERM; terminate_active; cancel_child; exit "$1"; }
trap on_exit EXIT
trap 'on_signal 129' HUP
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

DISPATCH_ATTEMPTED=1
DISPATCH_ARGS=(workflow run relink.yml -R "$REPO" -f target=kaggle
  -f "relink_request_id=$RELINK_REQUEST_ID")
[ -z "$LIBRARY_RUN_ID" ] || DISPATCH_ARGS+=(-f "library_run_id=$LIBRARY_RUN_ID")
[ -z "$PARENT_RUN_ATTEMPT" ] || DISPATCH_ARGS+=(-f "parent_run_attempt=$PARENT_RUN_ATTEMPT")
[ -z "$SEFARIA_TAG" ] || DISPATCH_ARGS+=(-f "sefaria_tag=$SEFARIA_TAG")
[ -z "$SNAPSHOT_SHA256" ] || DISPATCH_ARGS+=(-f "snapshot_sha256=$SNAPSHOT_SHA256")
[ -z "$SEFARIA_METADATA_SHA256" ] || \
  DISPATCH_ARGS+=(-f "sefaria_release_metadata_sha256=$SEFARIA_METADATA_SHA256")
[ -z "$ADOPT_FINGERPRINT" ] || DISPATCH_ARGS+=(-f "adopt_fingerprint=$ADOPT_FINGERPRINT")
[ -z "$NER_CHECKPOINT_SOURCE_RUN_ID" ] || \
  DISPATCH_ARGS+=(-f "ner_checkpoint_source_run_id=$NER_CHECKPOINT_SOURCE_RUN_ID")
[ -z "$NER_CHECKPOINT_SOURCE_RUN_ATTEMPT" ] || \
  DISPATCH_ARGS+=(-f "ner_checkpoint_source_run_attempt=$NER_CHECKPOINT_SOURCE_RUN_ATTEMPT")
[ -z "$NER_CHECKPOINT_SOURCE_ENGINE_FINGERPRINT" ] || \
  DISPATCH_ARGS+=(
    -f "ner_checkpoint_source_engine_fingerprint=$NER_CHECKPOINT_SOURCE_ENGINE_FINGERPRINT"
  )
[ -z "$RECOVERY_MODE" ] || DISPATCH_ARGS+=(-f recovery_mode=true)
[ -z "$DRY" ] || DISPATCH_ARGS+=(-f dry_run=true)
python3 "$HERE/scripts/exec_new_session.py" gh "${DISPATCH_ARGS[@]}" &
ACTIVE_PID=$!
wait "$ACTIVE_PID"
ACTIVE_PID=""

# Capture the exact child's databaseId before any further work (bounded retries for
# API eventual consistency), so cleanup cancels a known id instead of re-searching.
# The created>=dispatch window includes COMPLETED runs — a child that failed fast is
# still a discovery hit, not a "never appeared". Exactly one match is required: two
# runs sharing OUR fresh request id is a broken invariant — cancel them all via the
# trap, never pick one.
for _ in $(seq 1 24); do
  sleep 5
  # A transient listing error just means "retry next round" — the bounded loop plus
  # the final hard failure below keep this fail-loud, not fail-open.
  if ! ROWS="$(list_runs_all "$REPO" relink.yml)"; then
    continue
  fi
  MATCHES="$(printf '%s\n' "$ROWS" | awk -F'\t' -v t="$CHILD_TITLE" '$3 == t')"
  IDS="$(printf '%s\n' "$MATCHES" | awk -F'\t' 'NF {print $1}')"
  if [ -n "$IDS" ]; then
    read -ra ID_ARR <<<"$(echo $IDS)"
    if [ "${#ID_ARR[@]}" -gt 1 ]; then
      echo "::error::${#ID_ARR[@]} relink runs share request id $RELINK_REQUEST_ID — refusing to guess" >&2
      exit 1
    fi
    CHILD_RUN_ID="${ID_ARR[0]}"
    CHILD_STATUS="$(printf '%s\n' "$MATCHES" | awk -F'\t' -v id="$CHILD_RUN_ID" '$1 == id {print $2}')"
    break
  fi
done
[ -n "$CHILD_RUN_ID" ] || { echo "::error::queued relink ($CHILD_TITLE) never appeared in the API within 120s" >&2; exit 1; }
echo "relink queued: run $CHILD_RUN_ID ($CHILD_TITLE${DRY:+, dry_run})"
[ "$CHILD_STATUS" != completed ] || {
  echo "::error::relink child $CHILD_RUN_ID completed before Kaggle provisioning; refusing to boot a useless GPU session" >&2
  exit 1
}

PUSH=$(mktemp -d)
B64=$(base64 < "$HERE/kaggle/bootstrap.sh" | tr -d '\n')
cat > "$PUSH/run.py" <<PY
import base64, os, subprocess
os.makedirs("/kaggle/temp", exist_ok=True)
with open("/kaggle/temp/bootstrap.sh", "w") as fh:
    fh.write(base64.b64decode("$B64").decode())
env = dict(os.environ, JIT_CONFIG="$JIT")
subprocess.run(["bash", "/kaggle/temp/bootstrap.sh"], env=env, check=True)
PY
cat > "$PUSH/kernel-metadata.json" <<JSON
{
  "id": "$KERNEL_ID",
  "title": "linker-gpu-runner",
  "code_file": "run.py",
  "language": "python",
  "kernel_type": "script",
  "is_private": true,
  "enable_gpu": true,
  "enable_tpu": false,
  "enable_internet": true,
  "dataset_sources": [],
  "competition_sources": [],
  "kernel_sources": ["$TOOLS_KERNEL", "$RUNTIME_KERNEL"],
  "model_sources": []
}
JSON
# Managed push: run it as a tracked child and sit in `wait` — bash delivers a signal
# to the trap immediately while waiting on a builtin, and the handler kills the push
# before cancelling the queued child (a push left running could still boot a session).
PUSH_LOG=$(mktemp)
python3 "$HERE/scripts/exec_new_session.py" kaggle kernels push -p "$PUSH" >"$PUSH_LOG" 2>&1 &
ACTIVE_PID=$!
wait "$ACTIVE_PID"
ACTIVE_PID=""
cat "$PUSH_LOG"
if grep -q 'Kernel push error:' "$PUSH_LOG" ||
   ! grep -Eq 'Kernel version [0-9]+ successfully pushed' "$PUSH_LOG"; then
  echo "::error::Kaggle did not accept the kernel push" >&2
  rm -f "$PUSH_LOG"
  exit 1
fi
rm -f "$PUSH_LOG"
# Success: a session will boot and pick the child up. From this point a late signal or
# a failure in the tail must NOT cancel a live handoff — disarm everything.
trap - EXIT HUP INT TERM
rm -rf "$PUSH"

echo
echo "session status:  kaggle kernels status $KERNEL_ID"
echo "session log:     kaggle kernels output $KERNEL_ID -p /tmp/kaggle-log --force"
echo "job pickup:      gh run list -R $REPO -w relink.yml -L1"
