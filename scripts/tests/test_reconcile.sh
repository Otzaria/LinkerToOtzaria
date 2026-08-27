#!/usr/bin/env bash
# Acceptance tests for scripts/reconcile_pipeline.sh against a MOCKED gh.
#
#   • parent completed → child reaped;   • parent alive, same attempt → kept;
#   • parent rerun (current attempt > stamped) → normal child reaped, explicit
#     recovery kept;   • parent 404 → reaped;
#   • standalone / request=none / legacy titles → never touched;
#   • dispatcher + child sharing one id (cross-workflow) → NOT a duplicate;
#   • two live runs of the SAME workflow sharing an id → exit 1, ZERO cancels;
#   • LISTING failure → RED tick with zero decisions (fail-closed, the old
#     process-substitution form reported "0 orphans" in green);
#   • malformed / statusless / non-numeric-attempt parent JSON → red, run left;
#   • transient parent-lookup error → red tick, run left alone;
#   • failed cancel → red tick.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT="$HERE/../reconcile_pipeline.sh"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin" "$WORK/state"
export MOCK_DIR="$WORK/state" MOCK_LOG="$WORK/calls.log"

# list_runs_active queries 5 statuses via paginated REST; the mock answers only
# status=queued from the per-workflow fixture (3-col TSV: id, status, title) —
# dedup in the helper makes that equivalent to any distribution across statuses.
cat > "$WORK/bin/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >> "$MOCK_LOG"
all="$*"
case "$1 $2" in
  "api --paginate")
      [ -z "${MOCK_LIST_FAIL:-}" ] || { echo "gh: HTTP 500" >&2; exit 1; }
      if [[ "$all" == *"-f status=queued"* ]]; then
        wf="$(sed -E 's|.*/workflows/([^/]+)/runs.*|\1|' <<<"$all")"
        cat "$MOCK_DIR/list_${wf//.yml/}.tsv" 2>/dev/null || true
      fi ;;
  "api repos/PARENT/actions/runs/"*)
      id="${2##*/}"
      if [ -f "$MOCK_DIR/parent_$id.json" ]; then cat "$MOCK_DIR/parent_$id.json"
      elif [ "$id" = "999" ]; then echo "gh: HTTP 500 boom" >&2; exit 1
      else echo "gh: HTTP 404 Not Found" >&2; exit 1; fi ;;
  "run cancel")
      if grep -qx "$3" "$MOCK_DIR/cancel_fail" 2>/dev/null; then exit 1; fi
      echo "CANCELLED $3" >> "$MOCK_LOG" ;;
  *) : ;;
esac
EOF
chmod +x "$WORK/bin/gh"

run_reconcile() {
  : > "$MOCK_LOG"
  local rc=0
  ( export PATH="$WORK/bin:$PATH" RECONCILE_LINKER_REPO=LINKER RECONCILE_PARENT_REPO=PARENT
    bash "$SCRIPT" ) > "$WORK/out.txt" 2>&1 || rc=$?
  echo "$rc"
}
cancelled() { grep -c "CANCELLED $1" "$MOCK_LOG" 2>/dev/null || true; }
PASS=0; FAIL=0
check() { local name="$1"; shift; if "$@"; then echo "ok   $name"; PASS=$((PASS+1)); else echo "FAIL $name"; FAIL=$((FAIL+1)); fi; }
row() { printf '%s\tqueued\t%s\n' "$1" "$2"; }

RA=$(printf 'a%.0s' {1..64}); RB=$(printf 'b%.0s' {1..64}); RC64=$(printf 'c%.0s' {1..64})
RD=$(printf 'd%.0s' {1..64}); RE=$(printf 'e%.0s' {1..64})
RF=$(printf 'f%.0s' {1..64}); RG=$(printf '1%.0s' {1..64}); RH=$(printf '2%.0s' {1..64})

# Scenario matrix — one pass, mixed population.
{ row 101 "relink request=$RA parent=11:1"
  row 102 "relink request=$RB parent=12:1"
  row 103 "relink request=$RC64 parent=13:2"
  row 104 "relink request=$RD parent=14:1"
  row 105 "relink request=$RE parent=standalone"
  row 106 "relink request=none parent=standalone"
  row 107 "relink (library_run_id=777)"
  row 108 "relink-recovery request=$RF parent=15:1"
  row 109 "relink-recovery request=$RG parent=16:1"
  row 110 "relink-recovery request=$RH parent=17:1"
} > "$MOCK_DIR/list_relink.tsv"
row 201 "kaggle-relink request=$RA parent=11:1" > "$MOCK_DIR/list_kaggle-relink.tsv"
echo '{"status":"completed","run_attempt":1}'   > "$MOCK_DIR/parent_11.json"
echo '{"status":"in_progress","run_attempt":1}' > "$MOCK_DIR/parent_12.json"
echo '{"status":"in_progress","run_attempt":3}' > "$MOCK_DIR/parent_13.json"
echo '{"status":"completed","run_attempt":1,"conclusion":"failure"}' > "$MOCK_DIR/parent_15.json"
echo '{"status":"completed","run_attempt":1,"conclusion":"success"}' > "$MOCK_DIR/parent_16.json"
echo '{"status":"completed","run_attempt":2,"conclusion":"failure"}' > "$MOCK_DIR/parent_17.json"

rc=$(run_reconcile)
check "parent completed → child + dispatcher reaped" test "$(cancelled 101)" -eq 1 -a "$(cancelled 201)" -eq 1
check "parent alive on stamped attempt → kept"        test "$(cancelled 102)" -eq 0
check "parent rerun superseded attempt → reaped"      test "$(cancelled 103)" -eq 1
check "parent 404 → reaped"                           test "$(cancelled 104)" -eq 1
check "standalone/none/legacy → untouched"            test "$(cancelled 105)" -eq 0 -a "$(cancelled 106)" -eq 0 -a "$(cancelled 107)" -eq 0
check "explicit failed-parent recovery → kept"        test "$(cancelled 108)" -eq 0
check "recovery of successful parent → reaped"        test "$(cancelled 109)" -eq 1
check "parent rerun does not cancel earlier recovery"  test "$(cancelled 110)" -eq 0
check "dispatcher+child same id ≠ duplicate; clean tick rc=0" test "$rc" -eq 0

# Fail-closed listing: EVERY status query fails → red tick, zero decisions.
: > "$MOCK_LOG"
rc=0
( export PATH="$WORK/bin:$PATH" RECONCILE_LINKER_REPO=LINKER RECONCILE_PARENT_REPO=PARENT MOCK_LIST_FAIL=1
  bash "$SCRIPT" ) > "$WORK/out.txt" 2>&1 || rc=$?
check "listing failure → rc=1, zero cancels, no 'reconcile complete'" \
  test "$rc" -eq 1 -a "$(grep -c CANCELLED "$MOCK_LOG")" -eq 0 -a "$(grep -c 'reconcile complete' "$WORK/out.txt")" -eq 0

# Malformed / statusless / bad-attempt parent JSON → red, run left alone.
row 120 "relink request=$RA parent=21:1" > "$MOCK_DIR/list_relink.tsv"
: > "$MOCK_DIR/list_kaggle-relink.tsv"
echo 'not json at all' > "$MOCK_DIR/parent_21.json"
rc=$(run_reconcile)
check "unparseable parent JSON → rc=1, no cancel" test "$rc" -eq 1 -a "$(cancelled 120)" -eq 0
echo '{"run_attempt":1}' > "$MOCK_DIR/parent_21.json"
rc=$(run_reconcile)
check "parent missing status → rc=1, no cancel" test "$rc" -eq 1 -a "$(cancelled 120)" -eq 0
echo '{"status":"completed","run_attempt":"x"}' > "$MOCK_DIR/parent_21.json"
rc=$(run_reconcile)
check "parent non-numeric attempt → rc=1, no cancel" test "$rc" -eq 1 -a "$(cancelled 120)" -eq 0
echo '{"status":"in_progress","run_attempt":1}' > "$MOCK_DIR/parent_21.json"
row 120 "relink request=$RA parent=21:2" > "$MOCK_DIR/list_relink.tsv"
rc=$(run_reconcile)
check "stamped future parent attempt → rc=1, no cancel" test "$rc" -eq 1 -a "$(cancelled 120)" -eq 0
rm -f "$MOCK_DIR/parent_21.json"

# Transient parent error → red tick, run left alone.
row 108 "relink request=$RA parent=999:1" > "$MOCK_DIR/list_relink.tsv"
rc=$(run_reconcile)
check "transient parent lookup → rc=1, no cancel" test "$rc" -eq 1 -a "$(cancelled 108)" -eq 0

# True duplicate: two live runs of the SAME workflow, one id, parent alive.
{ row 102 "relink request=$RB parent=12:1"
  row 110 "relink request=$RB parent=12:1"
} > "$MOCK_DIR/list_relink.tsv"
rc=$(run_reconcile)
check "same-workflow duplicate → rc=1, zero cancels" test "$rc" -eq 1 -a "$(grep -c CANCELLED "$MOCK_LOG")" -eq 0

# Cancel failure → red tick.
row 101 "relink request=$RA parent=11:1" > "$MOCK_DIR/list_relink.tsv"
echo "101" > "$MOCK_DIR/cancel_fail"
rc=$(run_reconcile)
check "failed cancel → rc=1" test "$rc" -eq 1
rm -f "$MOCK_DIR/cancel_fail"

echo "----"
echo "reconcile: $PASS passed, $FAIL failed"
exit "$((FAIL > 0))"
