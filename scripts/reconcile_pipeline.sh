#!/usr/bin/env bash
# Reconciler — the RECOVERY path of the relink identity contract (the parent build's
# inline cleanup is the fast path). Invoked after a failed/cancelled parent build,
# or manually, from reconcile-pipeline.yml.
#
# For every LIVE (pre-terminal) relink.yml / kaggle-relink.yml run stamped with a
# serial identity ("<wf> request=<64-hex> parent=<run_id>:<attempt>"), fetch the parent
# SeforimLibrary run and decide:
#   • parent completed (any conclusion)            → normal child: cancel.
#   • recovery child + same failed terminal attempt → keep: this is the explicit
#     replay contract, and no live build is expected to be waiting.
#   • parent's current attempt > stamped attempt   → that attempt was superseded by a
#     rerun (its request id is dead — a rerun derives a NEW id) — cancel.
#   • parent gone entirely (HTTP 404)              → deleted/never existed — cancel.
#   • parent alive on the stamped attempt          → leave it alone; the live build's
#     own wait-loop + cleanup govern it.
#   • two live runs of the SAME workflow sharing a request id → broken invariant —
#     fail LOUD (exit 1); never pick one heuristically. (The dispatcher and the child
#     it queued legitimately share one id ACROSS workflows.)
# Manual/standalone runs (parent=standalone, request=none, or any other title) are
# never touched — they are the operator's business and were never build-dispatched.
#
# FAIL-CLOSED LISTING: run enumeration goes through lib/gh_runs.sh — full REST
# pagination over every pre-terminal status (an old orphan can never be pushed out
# of a "-L 100" window by fresh completed runs), and ANY listing/API failure turns
# the invocation red instead of reporting "0 orphans" on a broken token. Parent JSON is
# validated (known status, positive-int run_attempt) before any decision.
#
# Every action is idempotent (cancel of a cancelling run is a no-op), so overlapping
# failure notifications or a simultaneous manual dispatch are safe.
set -euo pipefail

# shellcheck source=lib/gh_runs.sh
source "$(cd "$(dirname "$0")" && pwd)/lib/gh_runs.sh"

REPO=${RECONCILE_LINKER_REPO:-Otzaria/LinkerToOtzaria}
PARENT_REPO=${RECONCILE_PARENT_REPO:-Otzaria/SeforimLibrary}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

FAILURES=0
REAPED=0
SEEN_IDS_FILE="$TMP/seen-request-ids"
: > "$SEEN_IDS_FILE"

handle_run() {
  local wf="$1" rid="$2" title="$3"
  local req prun pattempt kind
  if [[ "$title" =~ ^(relink|relink-recovery|kaggle-relink)\ request=([0-9a-f]{64})\ parent=([1-9][0-9]*):([1-9][0-9]*)$ ]]; then
    kind="${BASH_REMATCH[1]}"; req="${BASH_REMATCH[2]}"; prun="${BASH_REMATCH[3]}"; pattempt="${BASH_REMATCH[4]}"
  else
    echo "skip  $wf/$rid: not a serial identity-stamped run ($title)"
    return 0
  fi
  if ! gh api "repos/$PARENT_REPO/actions/runs/$prun" > "$TMP/parent.json" 2> "$TMP/parent.err"; then
    if grep -q "HTTP 404" "$TMP/parent.err"; then
      echo "reap  $wf/$rid: parent run $prun does not exist — no build can be waiting"
      gh run cancel "$rid" -R "$REPO" || { echo "::warning::cancel of $wf/$rid failed"; FAILURES=$((FAILURES+1)); return 0; }
      REAPED=$((REAPED+1))
    else
      echo "::warning::parent lookup for $wf/$rid failed transiently — left for the next invocation"
      cat "$TMP/parent.err" >&2
      FAILURES=$((FAILURES+1))
    fi
    return 0
  fi

  # Validate the parent payload BEFORE acting on it: a malformed body, an unknown
  # status, or a non-numeric attempt must never drive a cancel decision.
  local pstatus pcurrent pconclusion
  if ! jq -e . "$TMP/parent.json" > /dev/null 2>&1; then
    echo "::warning::parent $prun returned unparseable JSON — left for the next invocation"
    FAILURES=$((FAILURES+1)); return 0
  fi
  pstatus=$(jq -r '.status // empty' "$TMP/parent.json")
  pcurrent=$(jq -r '.run_attempt // empty' "$TMP/parent.json")
  pconclusion=$(jq -r '.conclusion // empty' "$TMP/parent.json")
  case "$pstatus" in
    requested|waiting|pending|queued|in_progress|completed) ;;
    *) echo "::warning::parent $prun has unrecognized status '$pstatus' — left for the next invocation"
       FAILURES=$((FAILURES+1)); return 0 ;;
  esac
  if ! [[ "$pcurrent" =~ ^[1-9][0-9]*$ ]]; then
    echo "::warning::parent $prun has non-numeric run_attempt '$pcurrent' — left for the next invocation"
    FAILURES=$((FAILURES+1)); return 0
  fi

  if [ "$kind" = relink-recovery ] && [ "$pcurrent" -eq "$pattempt" ]; then
    if [ "$pstatus" != completed ]; then
      echo "::warning::recovery $wf/$rid has a non-terminal parent $prun:$pattempt — left untouched"
      FAILURES=$((FAILURES+1))
      return 0
    fi
    case "$pconclusion" in
      failure|cancelled|timed_out|action_required|startup_failure|stale)
        echo "keep  $wf/$rid: explicit recovery of failed parent $prun:$pattempt ($pconclusion)"
        return 0 ;;
      success|neutral|skipped)
        echo "reap  $wf/$rid: recovery parent $prun:$pattempt is not failed ($pconclusion)"
        gh run cancel "$rid" -R "$REPO" || { echo "::warning::cancel of $wf/$rid failed"; FAILURES=$((FAILURES+1)); return 0; }
        REAPED=$((REAPED+1)); return 0 ;;
      *)
        echo "::warning::recovery parent $prun:$pattempt has unknown conclusion '$pconclusion' — left untouched"
        FAILURES=$((FAILURES+1)); return 0 ;;
    esac
  elif [ "$pcurrent" -lt "$pattempt" ]; then
    echo "::warning::parent $prun current attempt $pcurrent is below stamped attempt $pattempt — left untouched"
    FAILURES=$((FAILURES+1))
  elif [ "$pstatus" = "completed" ] || [ "$pcurrent" -gt "$pattempt" ]; then
    echo "reap  $wf/$rid: parent $prun attempt $pattempt is over (status=$pstatus, current attempt=$pcurrent)"
    gh run cancel "$rid" -R "$REPO" || { echo "::warning::cancel of $wf/$rid failed"; FAILURES=$((FAILURES+1)); return 0; }
    REAPED=$((REAPED+1))
  else
    echo "keep  $wf/$rid: parent $prun attempt $pattempt still live (status=$pstatus)"
  fi
}

collect_workflow() {
  local wf="$1" rows rid status title
  # Capture-then-parse: a listing failure must surface as a RED invocation, never as an
  # empty scan (the old process-substitution form swallowed gh's exit status).
  if ! rows=$(list_runs_active "$REPO" "$wf"); then
    echo "::error::listing active $wf runs failed — cannot reconcile blind"
    FAILURES=$((FAILURES+1))
    return 0
  fi
  printf '%s\n' "$rows" > "$TMP/$wf.tsv"
  while IFS=$'\t' read -r rid status title; do
    [ -n "$rid" ] || continue
    if [[ "$title" =~ ^(relink|relink-recovery|kaggle-relink)\ request=([0-9a-f]{64})\ parent=([1-9][0-9]*):([1-9][0-9]*)$ ]]; then
      echo "$wf ${BASH_REMATCH[2]}" >> "$SEEN_IDS_FILE"
    fi
  done <<< "$rows"
}

collect_workflow relink.yml
collect_workflow kaggle-relink.yml

# Do not perform even one cancel until both listings are known-good and the
# duplicate invariant has been checked across the complete captured snapshot.
if [ "$FAILURES" -gt 0 ]; then
  echo "::error::$FAILURES reconcile listing(s) failed — zero actions taken"
  exit 1
fi

# Broken invariant: one request id on more than one live run of one workflow. Never
# guess a survivor — turn the invocation red so a human (or the parent build's own
# refuse-to-guess) resolves it.
DUPES=$(sort "$SEEN_IDS_FILE" | uniq -d)
if [ -n "$DUPES" ]; then
  echo "::error::multiple live runs share a request id:"
  echo "$DUPES"
  exit 1
fi

for wf in relink.yml kaggle-relink.yml; do
  while IFS=$'\t' read -r rid status title; do
    [ -n "$rid" ] || continue
    handle_run "$wf" "$rid" "$title"
  done < "$TMP/$wf.tsv"
done
if [ "$FAILURES" -gt 0 ]; then
  echo "::error::$FAILURES reconcile action(s) failed — retry with an exact manual invocation"
  exit 1
fi
echo "reconcile complete: $REAPED orphan(s) reaped."
