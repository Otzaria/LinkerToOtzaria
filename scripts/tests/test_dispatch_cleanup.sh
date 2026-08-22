#!/usr/bin/env bash
# Acceptance tests for scripts/dispatch_kaggle_relink.sh — the identity + cleanup
# contract, executed against MOCKED gh/kaggle binaries (no network, no tokens).
#
# The gh mock is a mini-simulator: it records the `gh workflow run` dispatch and
# answers the paginated REST listing queries with rows constructed from what was
# actually dispatched — so the exact-title discovery path is exercised for real,
# including the standalone case where the script derives its own request id.
#
# Covered here (the offline-executable slice of the acceptance list):
#   • admission: busy linker → refuse BEFORE any JIT/dispatch; listing FAILURE →
#     refuse (fail-closed, never provision blind);
#   • kernel-push failure → the queued child is cancelled, exit != 0;
#   • mktemp failure after dispatch → cancelled;
#   • SIGTERM mid-push → push killed, cancelled once (no double cleanup), exit 143;
#   • success → NO cancel;
#   • two runs sharing our request id → refuse to guess, cancel BOTH;
#   • standalone (operator) → 64-hex id derived, failure cancels the right child.
# NOT executable offline (canary-only, by design): queue:max survival of 3 rapid
# dispatches, rerun-gets-new-request-id across real attempts, late-appearing child
# reaped by the event-driven reconciler on real infrastructure.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT="$HERE/../dispatch_kaggle_relink.sh"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin" "$WORK/state"
export MOCK_DIR="$WORK/state"

cat > "$WORK/bin/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >> "$MOCK_LOG"
all="$*"
emit_child_row() {  # constructed from the recorded dispatch; 3- or 5-col
  local cols="$1" title
  [ -f "$MOCK_DIR/dispatched.env" ] || return 0
  # shellcheck source=/dev/null
  source "$MOCK_DIR/dispatched.env"
  if [ -n "${D_LIBRARY_RUN_ID:-}" ]; then
    prefix=relink; [ -z "${D_RECOVERY_MODE:-}" ] || prefix=relink-recovery
    title="$prefix request=$D_REQUEST_ID parent=$D_LIBRARY_RUN_ID:$D_PARENT_ATTEMPT"
  else
    title="relink request=$D_REQUEST_ID parent=standalone"
  fi
  if [ "$cols" = 5 ]; then
    printf '77001\tqueued\t%s\tSHA1234\t2026-01-01T00:00:00Z\n' "$title"
    [ -z "${MOCK_DUP_CHILD:-}" ] || printf '77002\tqueued\t%s\tSHA1234\t2026-01-01T00:00:01Z\n' "$title"
  else
    printf '77001\tqueued\t%s\n' "$title"
    [ -z "${MOCK_DUP_CHILD:-}" ] || printf '77002\tqueued\t%s\n' "$title"
  fi
}
case "$1 $2" in
  "api --paginate")
      [ -z "${MOCK_LIST_FAIL:-}" ] || { echo "gh: HTTP 500" >&2; exit 1; }
      if [[ "$all" == *"actions/runners"* ]]; then
        [ -z "${MOCK_STALE_RUNNER:-}" ] || printf '44\tkaggle-old\tfalse\toffline\n'
      elif [[ "$all" == *"-f created="* ]]; then
        emit_child_row 5
      elif [[ "$all" == *"-f status=queued"* ]]; then
        if [ -f "$MOCK_DIR/dispatched.env" ]; then emit_child_row 3
        else n="${MOCK_BUSY:-0}"; i=0; while [ "$i" -lt "$n" ]; do printf '5%02d\tqueued\tbusy-run-%d\n' "$i" "$i"; i=$((i+1)); done; fi
      elif [[ "$all" == *"workflows/relink.yml/runs"* ]]; then
        emit_child_row 5
      fi ;;
  "api -X")
      if [[ "$all" == *" DELETE "* ]]; then echo "DELETED" >> "$MOCK_LOG"
      else echo "FAKEJIT"
      fi ;;
  "workflow run")
      rid=""; lib=""; att=""; recovery=""
      while [ $# -gt 0 ]; do
        case "$1" in
          relink_request_id=*) rid="${1#*=}" ;;
          library_run_id=*) lib="${1#*=}" ;;
          parent_run_attempt=*) att="${1#*=}" ;;
          recovery_mode=true) recovery=1 ;;
        esac; shift
      done
      printf 'D_REQUEST_ID=%s\nD_LIBRARY_RUN_ID=%s\nD_PARENT_ATTEMPT=%s\nD_RECOVERY_MODE=%s\n' "$rid" "$lib" "$att" "$recovery" > "$MOCK_DIR/dispatched.env"
      exit 0 ;;
  "run cancel") echo "CANCELLED $3" >> "$MOCK_LOG"; exit "${MOCK_CANCEL_RC:-0}" ;;
  *) exit 0 ;;
esac
EOF
cat > "$WORK/bin/kaggle" <<'EOF'
#!/usr/bin/env bash
echo "kaggle $*" >> "$MOCK_LOG"
/bin/sleep "${MOCK_KAGGLE_SLEEP:-0}"
[ -z "${MOCK_KAGGLE_PUSH_ERROR:-}" ] || {
  echo "Kernel push error: Maximum weekly GPU quota reached."
  exit 0
}
[ "${MOCK_KAGGLE_RC:-0}" -ne 0 ] || echo "Kernel version 99 successfully pushed."
exit "${MOCK_KAGGLE_RC:-0}"
EOF
cat > "$WORK/bin/sleep" <<'EOF'
#!/usr/bin/env bash
/bin/sleep 0.05
EOF
chmod +x "$WORK/bin/"*

RID=$(printf 'a%.0s' {1..64})
PASS=0; FAIL=0
check() {
  local name="$1"; shift
  if "$@"; then echo "ok   $name"; PASS=$((PASS+1)); else echo "FAIL $name"; FAIL=$((FAIL+1)); fi
}
cancels() { grep -c CANCELLED "$MOCK_LOG" 2>/dev/null || true; }
reset_state() { rm -f "$MOCK_DIR/dispatched.env"; : > "$MOCK_LOG"; }
serial_args=(--library-run-id 555 --relink-request-id "$RID" --parent-run-attempt 2
             --sefaria-tag T1 --snapshot-sha256 "$RID" --sefaria-release-metadata-sha256 "$RID")

export MOCK_LOG="$WORK/t1.log"; reset_state
rc=0; ( export PATH="$WORK/bin:$PATH" MOCK_BUSY=2; bash "$SCRIPT" "${serial_args[@]}" ) >/dev/null 2>&1 || rc=$?
check "admission: busy → refuse pre-JIT/pre-dispatch" \
  test "$rc" -ne 0 -a "$(grep -c 'api -X' "$MOCK_LOG")" -eq 0 -a "$(grep -c 'workflow run' "$MOCK_LOG")" -eq 0

export MOCK_LOG="$WORK/t2.log"; reset_state
rc=0; ( export PATH="$WORK/bin:$PATH" MOCK_LIST_FAIL=1; bash "$SCRIPT" "${serial_args[@]}" ) >/dev/null 2>&1 || rc=$?
check "admission: listing failure → refuse (fail-closed)" \
  test "$rc" -ne 0 -a "$(grep -c 'workflow run' "$MOCK_LOG")" -eq 0

export MOCK_LOG="$WORK/t3.log"; reset_state
rc=0; ( export PATH="$WORK/bin:$PATH" MOCK_KAGGLE_RC=1; bash "$SCRIPT" "${serial_args[@]}" ) >/dev/null 2>&1 || rc=$?
check "push failure → child cancelled once, rc!=0" test "$rc" -ne 0 -a "$(cancels)" -eq 1

export MOCK_LOG="$WORK/t3-semantic.log"; reset_state
rc=0; ( export PATH="$WORK/bin:$PATH" MOCK_KAGGLE_PUSH_ERROR=1; bash "$SCRIPT" "${serial_args[@]}" ) >/dev/null 2>&1 || rc=$?
check "semantic push error with rc=0 → child cancelled" test "$rc" -ne 0 -a "$(cancels)" -eq 1

export MOCK_LOG="$WORK/t4.log"; reset_state
rc=0; ( export PATH="$WORK/bin:$PATH"; bash "$SCRIPT" "${serial_args[@]}" ) >/dev/null 2>&1 || rc=$?
check "success → no cancel, rc=0" test "$rc" -eq 0 -a "$(cancels)" -eq 0
check "request-specific JIT label → stale listener cannot steal child" \
  test "$(grep -c "labels\[\]=request-$RID" "$MOCK_LOG")" -eq 1

export MOCK_LOG="$WORK/t4-stale.log"; reset_state
rc=0; ( export PATH="$WORK/bin:$PATH" MOCK_STALE_RUNNER=1; bash "$SCRIPT" "${serial_args[@]}" ) >/dev/null 2>&1 || rc=$?
check "idle stale JIT registration → removed before new JIT" \
  test "$rc" -eq 0 -a "$(grep -c DELETED "$MOCK_LOG")" -eq 1

export MOCK_LOG="$WORK/t4-recovery.log"; reset_state
rc=0; ( export PATH="$WORK/bin:$PATH"; bash "$SCRIPT" "${serial_args[@]}" --recovery-mode ) >/dev/null 2>&1 || rc=$?
check "recovery → distinct exact title and workflow input" \
  test "$rc" -eq 0 -a "$(cancels)" -eq 0 -a "$(grep -c 'recovery_mode=true' "$MOCK_LOG")" -eq 1

export MOCK_LOG="$WORK/t4-adopt.log"; reset_state
rc=0; ( export PATH="$WORK/bin:$PATH"; bash "$SCRIPT" "${serial_args[@]}" \
  --recovery-mode --adopt-fingerprint 'old fingerprint::new fingerprint' ) >/dev/null 2>&1 || rc=$?
check "adoption attestation → quoted workflow input" \
  test "$rc" -eq 0 -a "$(grep -c 'adopt_fingerprint=old fingerprint::new fingerprint' "$MOCK_LOG")" -eq 1

export MOCK_LOG="$WORK/t4-checkpoint.log"; reset_state
rc=0; ( export PATH="$WORK/bin:$PATH"; bash "$SCRIPT" "${serial_args[@]}" \
  --recovery-mode \
  --ner-checkpoint-source-run-id 123 \
  --ner-checkpoint-source-run-attempt 2 \
  --ner-checkpoint-source-engine-fingerprint 'old engine fingerprint' \
  ) >/dev/null 2>&1 || rc=$?
check "checkpoint recovery identity → forwarded atomically" \
  test "$rc" -eq 0 \
    -a "$(grep -c 'ner_checkpoint_source_run_id=123' "$MOCK_LOG")" -eq 1 \
    -a "$(grep -c 'ner_checkpoint_source_run_attempt=2' "$MOCK_LOG")" -eq 1 \
    -a "$(grep -c 'ner_checkpoint_source_engine_fingerprint=old engine fingerprint' "$MOCK_LOG")" -eq 1

cat > "$WORK/bin/mktemp" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$WORK/bin/mktemp"
export MOCK_LOG="$WORK/t5.log"; reset_state
rc=0; ( export PATH="$WORK/bin:$PATH"; bash "$SCRIPT" "${serial_args[@]}" ) >/dev/null 2>&1 || rc=$?
check "mktemp failure after dispatch → cancelled" test "$rc" -ne 0 -a "$(cancels)" -eq 1
rm "$WORK/bin/mktemp"

export MOCK_LOG="$WORK/t6.log"; reset_state
( export PATH="$WORK/bin:$PATH" MOCK_KAGGLE_SLEEP=2; exec bash "$SCRIPT" "${serial_args[@]}" ) >/dev/null 2>&1 &
PID=$!
/bin/sleep 1.2
kill -TERM "$PID" 2>/dev/null
rc=0; wait "$PID" || rc=$?
check "SIGTERM mid-push → single cleanup, rc=143" test "$rc" -eq 143 -a "$(cancels)" -eq 1

export MOCK_LOG="$WORK/t7.log"; reset_state
rc=0; ( export PATH="$WORK/bin:$PATH" MOCK_DUP_CHILD=1; bash "$SCRIPT" "${serial_args[@]}" ) >/dev/null 2>&1 || rc=$?
check "duplicate ids → refuse to guess, cancel both" \
  test "$rc" -ne 0 -a "$(grep -c 'CANCELLED 77001' "$MOCK_LOG")" -eq 1 -a "$(grep -c 'CANCELLED 77002' "$MOCK_LOG")" -eq 1

export MOCK_LOG="$WORK/t8.log"; reset_state
rc=0; OUT=$( export PATH="$WORK/bin:$PATH" MOCK_KAGGLE_RC=1; bash "$SCRIPT" 2>&1 ) || rc=$?
check "standalone: derived 64-hex id + cancel on failure" \
  test "$rc" -ne 0 -a "$(cancels)" -eq 1 -a -n "$(grep -oE 'request=[0-9a-f]{64}' <<<"$OUT" | head -1)"

export MOCK_LOG="$WORK/t9.log"; reset_state
rc=0; ( export PATH="$WORK/bin:$PATH" MOCK_KAGGLE_RC=1 MOCK_CANCEL_RC=1; \
  bash "$SCRIPT" "${serial_args[@]}" ) >/dev/null 2>&1 || rc=$?
check "failed inline cancel → event-driven reconciler woken once" \
  test "$rc" -ne 0 -a \
    "$(grep -c 'workflow run reconcile-pipeline.yml' "$MOCK_LOG")" -eq 1

echo "----"
echo "dispatch cleanup: $PASS passed, $FAIL failed"
exit "$((FAIL > 0))"
