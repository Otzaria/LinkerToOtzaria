# Shared run-listing helpers — FULL server-side pagination via REST, never a
# window of "the last N runs" (an old orphan must not be pushed out of view by
# fresh completed runs). Callers MUST check the exit status: a listing failure
# is a loud failure, never an empty result.
#
# Requires: gh (authenticated). Source this file; do not execute it.

# list_runs_active <repo> <workflow-file>
#   TSV rows: id <TAB> status <TAB> display_title — every run in any
#   pre-terminal state (requested/waiting/pending/queued/in_progress), fully
#   paginated per status, de-duplicated by id (a run may transition between
#   the per-status queries). Exit 1 on any API failure (incl. workflow 404).
list_runs_active() {
  local repo="$1" wf="$2" pass status chunk out=""
  # Status-filtered endpoints are not one atomic snapshot. Two complete sweeps
  # close the common transition race (a run moving into a state whose query
  # already passed); dedup makes the union stable and side-effect free.
  for pass in 1 2; do
    for status in requested waiting pending queued in_progress; do
      chunk=$(gh api --paginate -X GET "repos/$repo/actions/workflows/$wf/runs" \
        -f status="$status" -f per_page=100 \
        --jq '.workflow_runs[] | [(.id|tostring), .status, .display_title] | @tsv') || return 1
      [ -z "$chunk" ] || out+="$chunk"$'\n'
    done
  done
  printf '%s' "$out" | awk -F'\t' 'NF && !seen[$1]++'
}

# list_runs_all <repo> <workflow-file>
# Full history, paginated, with no client-clock lower bound. Exact request ids in
# display_title are the identity; a skewed runner clock must not hide a fresh run.
list_runs_all() {
  local rows
  rows=$(gh api --paginate -X GET "repos/$1/actions/workflows/$2/runs" -f per_page=100 \
    --jq '.workflow_runs[] | [(.id|tostring), .status, .display_title, .head_sha, .created_at] | @tsv') || return 1
  printf '%s\n' "$rows" | awk -F'\t' 'NF && !seen[$1]++'
}

# list_runs_since <repo> <workflow-file> <iso8601-utc>
#   TSV rows: id <TAB> status <TAB> display_title <TAB> head_sha <TAB> created_at
#   for EVERY run (any status, completed included — a child that failed fast is
#   still a discovery hit) created at/after the given instant. Exit 1 on failure.
list_runs_since() {
  local repo="$1" wf="$2" since="$3" rows
  rows=$(gh api --paginate -X GET "repos/$repo/actions/workflows/$wf/runs" \
    -f created=">=$since" -f per_page=100 \
    --jq '.workflow_runs[] | [(.id|tostring), .status, .display_title, .head_sha, .created_at] | @tsv') || return 1
  printf '%s\n' "$rows" | awk -F'\t' 'NF && !seen[$1]++'
}

# count_runs_active <repo> <workflow-file> — prints the count; exit 1 on failure.
count_runs_active() {
  local rows
  rows=$(list_runs_active "$1" "$2") || return 1
  if [ -z "$rows" ]; then echo 0; else printf '%s\n' "$rows" | wc -l | tr -d ' '; fi
}
