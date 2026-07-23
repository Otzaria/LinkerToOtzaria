#!/usr/bin/env bash
# Bring up the Sefaria linker stack on a self-hosted Linux runner, idempotently.
# Mirrors the validated local POC: MongoDB + restored Sefaria dump, the gpu-server
# NER microservice (gunicorn on :5051), and Sefaria-Project (Django) in a venv.
#
# Heavy inputs (Mongo dump, spaCy model wheels) are cached under $CACHE keyed by their
# source URL, so only the first run — or a URL change — pays the download.
#
# Emits $STACK/engine_fingerprint.txt: the identity of everything that shapes linker
# output (Sefaria/gpu-server commits + model wheel hashes). The workflow folds it into
# --engine-fingerprint, so any engine drift forces a FULL relink instead of silently
# mixing engine versions inside the artifact store.
set -euo pipefail

STACK="${GITHUB_WORKSPACE:-$PWD}/stack"
CACHE="${LINKER_CACHE_DIR:-$HOME/.cache/linker-stack}"
SEF="$STACK/Sefaria-Project"
GPU="$STACK/gpu-server"
NER_URL="http://127.0.0.1:5051/recognize-entities"
STACK_ROLE="${LINKER_STACK_ROLE:-full}"
RUNTIME_LOCK_DIR="${LINKER_REPO:-$PWD}/ci/runtime-lock"
RUNTIME_LOCK_MANIFEST="$RUNTIME_LOCK_DIR/runtime-manifest.json"
RUNTIME_LOCK_SEFARIA="$RUNTIME_LOCK_DIR/sefaria.txt"
case "$STACK_ROLE" in
  full|ner|resolver) ;;
  *) echo "::error::LINKER_STACK_ROLE must be full, ner, or resolver"; exit 2 ;;
esac
mkdir -p "$STACK" "$CACHE"

# Kaggle chooses the mount directory for a kernel output. Discover by the pinned
# archive filename under the fixed input root and require exactly one match;
# never guess among duplicates or silently fall back to a network build.
if [ -z "${LINKER_RUNTIME_ARCHIVE:-}" ] && [ -n "${LINKER_RUNTIME_ROOT:-}" ]; then
  [ -d "$LINKER_RUNTIME_ROOT" ] || { echo "::error::runtime input root missing: $LINKER_RUNTIME_ROOT"; exit 1; }
  RUNTIME_MATCHES=$(find "$LINKER_RUNTIME_ROOT" -mindepth 2 -maxdepth 5 -type f \
    -name linker-python-runtime-v1.tar.zst -print)
  RUNTIME_MATCH_COUNT=$(printf '%s\n' "$RUNTIME_MATCHES" | awk 'NF' | wc -l | tr -d ' ')
  [ "$RUNTIME_MATCH_COUNT" -eq 1 ] || {
    echo "::error::expected exactly one linker-python-runtime-v1.tar.zst under $LINKER_RUNTIME_ROOT; found $RUNTIME_MATCH_COUNT"
    exit 1
  }
  LINKER_RUNTIME_ARCHIVE=$RUNTIME_MATCHES
  export LINKER_RUNTIME_ARCHIVE
fi

# Sefaria's requirements need Python >=3.10 (validated locally on 3.12); the runner's
# default python3 may be older. Explicit discovery, loud failure — never a silent
# fallback to an interpreter that cannot even install the requirements.
PYBIN="${LINKER_PYTHON:-}"
# Existence is not enough: a candidate must be able to CREATE a venv with pip —
# Kaggle ships a python3.12 whose ensurepip is broken (no python3.12-venv), which
# passed the old check and blew up mid-setup. Probe for real, pick the first that works.
venv_capable() {
  local probe; probe=$(mktemp -d)
  "$1" -m venv "$probe/v" >/dev/null 2>&1 && [ -x "$probe/v/bin/pip" ]
  local rc=$?; rm -rf "$probe"; return $rc
}
if [ -z "$PYBIN" ] && [ -n "${LINKER_RUNTIME_ARCHIVE:-}" ]; then
  command -v python3.12 >/dev/null 2>&1 || {
    echo "::error::the prebuilt runtime requires python3.12"; exit 1;
  }
  python3.12 -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3,12) else 1)' || {
    echo "::error::the prebuilt runtime requires Python 3.12.x"; exit 1;
  }
  PYBIN=python3.12
fi
if [ -z "$PYBIN" ]; then
  for cand in python3.12 python3.11 python3.10 python3; do
    command -v "$cand" >/dev/null 2>&1 || continue
    "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null || continue
    venv_capable "$cand" && PYBIN="$cand" && break
    echo "skipping $cand: cannot create a working venv (ensurepip broken?)"
  done
fi
[ -n "$PYBIN" ] || { echo "::error::no venv-capable Python >=3.10 on this runner (Sefaria requirements demand it) — install python3.11+ or set LINKER_PYTHON"; exit 1; }
echo "using $PYBIN ($($PYBIN --version))"

url_key() { printf '%s' "$1" | sha256sum | cut -c1-12; }

# ── 1. Sefaria-Project + gpu-server checkouts ────────────────────────────────
# Optional pins via SEFARIA_COMMIT / GPU_SERVER_COMMIT; unpinned drift is still safe —
# the fingerprint below changes with HEAD and triggers a full relink.
[ -d "$SEF" ] || git clone https://github.com/Sefaria/Sefaria-Project "$SEF"
[ -d "$GPU" ] || git clone https://github.com/Sefaria/gpu-server "$GPU"
[ -z "${SEFARIA_COMMIT:-}" ] || { git -C "$SEF" checkout -- .; git -C "$SEF" -c advice.detachedHead=false checkout "$SEFARIA_COMMIT"; }
[ -z "${GPU_SERVER_COMMIT:-}" ] || git -C "$GPU" -c advice.detachedHead=false checkout "$GPU_SERVER_COMMIT"

# Resolver fixes we maintain on top of the pin (see the patch header): two upstream
# crashes (empty-section pad, None-ref dedup) that took whole LINES down — with
# fail-loud they would fail every run touching those books. Idempotent apply;
# the patch hash is part of the engine fingerprint below.
PATCH="${LINKER_REPO:-$PWD}/ci/sefaria_resolver.patch"
if ! git -C "$SEF" apply --reverse --check "$PATCH" 2>/dev/null; then
  git -C "$SEF" apply "$PATCH"
fi

# ── 2. Python venv (numpy<2 — thinc/spaCy binary compat, learned in the POC) ─
# The venv carries an identity (python + requirements hash + commit); any change
# rebuilds it from scratch — a SEFARIA_COMMIT bump must never run on a stale venv.
SEF_VENV_BASE_ID="$($PYBIN --version 2>&1):$(sha256sum "$SEF/requirements.txt" | cut -c1-12):${SEFARIA_COMMIT:-head}"
GPU_VENV_ID="$($PYBIN --version 2>&1):$(sha256sum "$GPU/app/requirements.txt" | cut -c1-12):${GPU_SERVER_COMMIT:-head}"
SEF_VENV_ID="$SEF_VENV_BASE_ID"
CANONICAL_PYTHON_RUNTIME_ID=
if [ "$STACK_ROLE" = resolver ]; then
  # Kaggle's x86_64 wheels cannot be copied into the ARM resolver, but resolving
  # unpinned transitive dependencies again is not equivalent either.  Install the
  # exact Sefaria package versions from the verified Kaggle runtime manifest and
  # use that manifest's combined NER+resolver identity.  The GPU half is represented
  # by the checksum-gated raw-NER handoff and is deliberately not installed on ARM.
  CANONICAL_PYTHON_RUNTIME_ID=$(
    python3 "${LINKER_REPO:-$PWD}/ci/validate_runtime_lock.py" \
      --manifest "$RUNTIME_LOCK_MANIFEST" \
      --sefaria-freeze "$RUNTIME_LOCK_SEFARIA" \
      --sefaria-repo "$SEF" \
      --gpu-repo "$GPU" \
      --python-version "$($PYBIN --version 2>&1)"
  )
  [[ "$CANONICAL_PYTHON_RUNTIME_ID" =~ ^[0-9a-f]{16}$ ]]
  SEF_VENV_ID="$SEF_VENV_BASE_ID:resolver-lock-$(sha256sum "$RUNTIME_LOCK_SEFARIA" | cut -c1-12)"
fi

# Ephemeral Kaggle sessions receive both complete venvs as one immutable kernel
# output. Verify and install them together before either slow network fallback can
# run. Server runners may omit these variables and keep their persistent cache.
if [ -n "${LINKER_RUNTIME_ARCHIVE:-}${LINKER_RUNTIME_SHA256:-}" ]; then
  [ -n "${LINKER_RUNTIME_ARCHIVE:-}" ] && [ -n "${LINKER_RUNTIME_SHA256:-}" ] || {
    echo "::error::LINKER_RUNTIME_ARCHIVE and LINKER_RUNTIME_SHA256 must be set together"
    exit 1
  }
  if [ ! -x "$SEF/.venv/bin/python" ] || [ "$(cat "$SEF/.venv/.identity" 2>/dev/null)" != "$SEF_VENV_ID" ] || \
     [ ! -x "$GPU/.venv/bin/python" ] || [ "$(cat "$GPU/.venv/.identity" 2>/dev/null)" != "$GPU_VENV_ID" ]; then
    bash "${LINKER_REPO:-$PWD}/ci/install_prebuilt_runtime.sh" \
      "$LINKER_RUNTIME_ARCHIVE" "$LINKER_RUNTIME_SHA256" "$STACK" "$SEF_VENV_ID" "$GPU_VENV_ID"
  fi
fi
if [ ! -x "$SEF/.venv/bin/python" ] || [ "$(cat "$SEF/.venv/.identity" 2>/dev/null)" != "$SEF_VENV_ID" ]; then
  rm -rf "$SEF/.venv"
  "$PYBIN" -m venv "$SEF/.venv"
  "$SEF/.venv/bin/pip" install --upgrade pip
  if [ "$STACK_ROLE" = resolver ]; then
    "$SEF/.venv/bin/pip" install -r "$RUNTIME_LOCK_SEFARIA"
  else
    "$SEF/.venv/bin/pip" install -r "$SEF/requirements.txt"
    "$SEF/.venv/bin/pip" install "numpy<2"
  fi
  printf '%s' "$SEF_VENV_ID" > "$SEF/.venv/.identity"
fi
# local_settings: point Django at the local Mongo; secrets stay empty (offline linking).
if [ ! -f "$SEF/sefaria/local_settings.py" ]; then
  cp "$SEF/sefaria/local_settings_example.py" "$SEF/sefaria/local_settings.py"
fi
# Enforce the linker endpoint on EVERY run — the example defaults to a disabled
# linker on :5000, and Sefaria's bulk NER calls silently target it (every book
# then fails with connection-refused). :5051 is where setup starts gunicorn.
LS="$SEF/sefaria/local_settings.py"
sed -i "s|^ENABLE_LINKER = .*|ENABLE_LINKER = True|" "$LS"
sed -i "s|^GPU_SERVER_URL = .*|GPU_SERVER_URL = 'http://localhost:5051'|" "$LS"
grep -q "^ENABLE_LINKER = True$" "$LS" && grep -q "^GPU_SERVER_URL = 'http://localhost:5051'$" "$LS" \
  || { echo "::error::local_settings.py no longer carries ENABLE_LINKER/GPU_SERVER_URL as expected — upstream layout changed"; exit 1; }

# ── 3. MongoDB + Sefaria dump (this repo's dump release, cached per tag) ────
# Same pattern as the models: a fixed release in THIS repo (split into <2GiB parts
# for GitHub's asset cap), downloaded with gh, sha256-verified. No URL secret.
# The archive is extracted into a FRESH directory and every database it carries is
# dropped up-front — stale BSON from a previous archive, or collections that left the
# new dump, must not survive into the restored state.
DUMP_TAG="${LINKER_DUMP_TAG:-dump-v1}"
DUMP_REPO="${GITHUB_REPOSITORY:-Otzaria/LinkerToOtzaria}"
# Content-addressed dump identity: ALWAYS computed from the SHA256SUMS asset,
# BEFORE any cache decision. A tag-keyed marker once let a stale server cache
# fingerprint the same dump differently than a fresh Kaggle session (empty
# .dump-content-id → tag-key fallback) and broke a serial build. The restore
# marker is keyed by this content id, so re-tagged dump assets re-restore and
# every environment mints the identical fingerprint token. No fallback.
rm -rf "$CACHE/dump-sums" && mkdir -p "$CACHE/dump-sums"
gh release download "$DUMP_TAG" -R "$DUMP_REPO" -p SHA256SUMS -D "$CACHE/dump-sums"
DUMP_CONTENT_ID="$(sha256sum "$CACHE/dump-sums/SHA256SUMS" | cut -c1-16)"
DUMP_MARKER="$CACHE/.dump-restored-content-$DUMP_CONTENT_ID"
if [ "$STACK_ROLE" != ner ]; then
  pgrep -x mongod >/dev/null || (mkdir -p "$CACHE/mongo-data" && mongod --dbpath "$CACHE/mongo-data" --fork --logpath "$CACHE/mongod.log")
fi
if [ "$STACK_ROLE" != ner ] && [ ! -f "$DUMP_MARKER" ]; then
  rm -rf "$CACHE/dump-dl" && mkdir -p "$CACHE/dump-dl"
  gh release download "$DUMP_TAG" -R "$DUMP_REPO" -p 'dump.tar.gz.part-*' -D "$CACHE/dump-dl"
  cp "$CACHE/dump-sums/SHA256SUMS" "$CACHE/dump-dl/SHA256SUMS"
  cat "$CACHE/dump-dl"/dump.tar.gz.part-* > "$CACHE/dump-dl/dump.tar.gz"
  (cd "$CACHE/dump-dl" && sha256sum -c SHA256SUMS)
  rm -rf "$CACHE/dump-extract" && mkdir -p "$CACHE/dump-extract"
  tar -xzf "$CACHE/dump-dl/dump.tar.gz" -C "$CACHE/dump-extract"
  DUMP_DIR=$(find "$CACHE/dump-extract" -type d -name dump | head -1)
  [ -n "$DUMP_DIR" ] || DUMP_DIR="$CACHE/dump-extract"
  for dbdir in "$DUMP_DIR"/*/; do
    dbname=$(basename "$dbdir")
    mongosh --quiet --eval "db.getSiblingDB('$dbname').dropDatabase()" >/dev/null
  done
  mongorestore --gzip --drop "$DUMP_DIR"
  rm -rf "$CACHE/dump-dl" "$CACHE/dump-extract"
  rm -f "$CACHE"/.dump-restored-* "$CACHE/.dump-content-id"
  touch "$DUMP_MARKER"
fi

# ── 4. NER model wheels (cached per release tag) ─────────────────────────────
# Downloaded with gh from THIS repo's models release (the repo is private, so a raw
# URL would need auth anyway; gh already carries GH_TOKEN). The archive ships
# EXPECTED_SHA256.txt — verified after extraction, so a corrupt download fails loudly.
MODELS_TAG="${LINKER_MODELS_TAG:-models-v1}"
MODELS_REPO="${GITHUB_REPOSITORY:-Otzaria/LinkerToOtzaria}"
MODELS="$CACHE/models-$(url_key "$MODELS_REPO@$MODELS_TAG")"
if [ ! -f "$MODELS/.ready" ]; then
  rm -rf "$MODELS" && mkdir -p "$MODELS"
  gh release download "$MODELS_TAG" -R "$MODELS_REPO" -p 'linker_models.tar.gz' -D "$MODELS"
  tar -xzf "$MODELS/linker_models.tar.gz" -C "$MODELS"
  (cd "$MODELS" && sha256sum -c EXPECTED_SHA256.txt)
  for name in he_ref_ner he_subref_ner; do
    whl=$(find "$MODELS" -name "${name}-*.whl" | head -1)
    [ -n "$whl" ] || { echo "::error::${name} wheel missing from the $MODELS_TAG archive"; exit 1; }
    unzip -qo "$whl" -d "$MODELS/${name}_pkg"
  done
  rm -f "$MODELS/linker_models.tar.gz"
  touch "$MODELS/.ready"
fi
model_path() { find "$MODELS/$1_pkg/$1" -maxdepth 1 -mindepth 1 -type d -name "$1-*" | head -1; }
REF_MODEL=$(model_path he_ref_ner)
SUBREF_MODEL=$(model_path he_subref_ner)
[ -n "$REF_MODEL" ] && [ -n "$SUBREF_MODEL" ] || { echo "::error::model dirs not found after unzip"; exit 1; }

# ── 5. gpu-server venv + generated config + gunicorn on :5051 ────────────────
# app/local_config.py is generated here (it is machine-specific, never committed).
if [ "$STACK_ROLE" != resolver ] && \
   { [ ! -x "$GPU/.venv/bin/gunicorn" ] || [ "$(cat "$GPU/.venv/.identity" 2>/dev/null)" != "$GPU_VENV_ID" ]; }; then
  rm -rf "$GPU/.venv"
  "$PYBIN" -m venv "$GPU/.venv"
  "$GPU/.venv/bin/pip" install --upgrade pip
  # gunicorn is the serving runtime, not an app dependency — requirements.txt omits it.
  "$GPU/.venv/bin/pip" install -r "$GPU/app/requirements.txt" gunicorn
  printf '%s' "$GPU_VENV_ID" > "$GPU/.venv/.identity"
fi

# Resolve and verify the semantic Python environment identity. The bundle SHA is
# a supply-chain pin; the freeze hashes are the engine identity, so package drift
# on either target forces an explicit full/adopt decision.  The split resolver
# verifies its locally installed Sefaria environment byte-for-byte against the
# producer runtime lock; its unused CUDA environment is represented by the exact
# handoff/runtime manifest rather than being installed on ARM.
if [ "$STACK_ROLE" = resolver ]; then
  freeze_tmp=$(mktemp)
  "$SEF/.venv/bin/python" -m pip freeze --all > "$freeze_tmp"
  cmp -s "$freeze_tmp" "$RUNTIME_LOCK_SEFARIA" || {
    echo "::error::ARM resolver packages disagree with the verified Kaggle Sefaria freeze"
    diff -u "$RUNTIME_LOCK_SEFARIA" "$freeze_tmp" || true
    rm -f "$freeze_tmp"
    exit 1
  }
  mv "$freeze_tmp" "$SEF/.venv/.freeze"
  PYTHON_RUNTIME_ID="$CANONICAL_PYTHON_RUNTIME_ID"
else
  for venv in "$SEF/.venv" "$GPU/.venv"; do
    freeze_tmp=$(mktemp)
    "$venv/bin/python" -m pip freeze --all > "$freeze_tmp"
    if [ -f "$venv/.freeze" ]; then
      cmp -s "$freeze_tmp" "$venv/.freeze" || {
        echo "::error::installed packages disagree with the verified runtime freeze: $venv"
        rm -f "$freeze_tmp"
        exit 1
      }
      rm -f "$freeze_tmp"
    else
      mv "$freeze_tmp" "$venv/.freeze"
    fi
  done
  SEF_FREEZE_SHA256=$(sha256sum "$SEF/.venv/.freeze" | cut -d' ' -f1)
  GPU_FREEZE_SHA256=$(sha256sum "$GPU/.venv/.freeze" | cut -d' ' -f1)
  PYTHON_RUNTIME_ID=$(printf '%s\n%s\n' "$SEF_FREEZE_SHA256" "$GPU_FREEZE_SHA256" | sha256sum | cut -c1-16)
fi
cat > "$GPU/app/local_config.py" <<EOF
MODEL_PATHS = [
    {"arch": "spacy", "lang": "he", "path": "$REF_MODEL", "type": "named_entity"},
    {"arch": "spacy", "lang": "he", "path": "$SUBREF_MODEL", "type": "ref_part"},
]
EOF
# A gunicorn from a previous run survives on a self-hosted runner with the OLD models
# still in memory — a health check alone would keep it and silently contradict the
# fingerprint. Restart whenever the NER service identity (models+gpu-server+config)
# differs from the one that launched the running process.
GUNICORN_WORKERS="${NER_WORKERS:-3}"
NER_ID="$MODELS_TAG:$(git -C "$GPU" rev-parse HEAD):$(sha256sum "$GPU/app/local_config.py" | cut -c1-12):w$GUNICORN_WORKERS"
NER_MARKER="$CACHE/.ner-identity"
NER_PIDFILE="$CACHE/gunicorn.pid"
NER_SCOPE="${LINKER_NER_SCOPE:-$CACHE/gunicorn.scope.json}"
NER_SCOPE_MODE="${LINKER_NER_SCOPE_MODE:-0600}"
PROCESS_SCOPE="$(dirname "$0")/process_scope.py"
if [ "$STACK_ROLE" = resolver ]; then
  # The CPU resolver deliberately has no model server in memory.  This runs under
  # the shared host lease, so stopping the exact owned group cannot race another job.
  if [ -f "$NER_SCOPE" ]; then
    bash "$(dirname "$0")/stop_ner.sh"
  fi
elif [ "$(cat "$NER_MARKER" 2>/dev/null)" != "$NER_ID" ]; then
  # Stop only an identity-bound process group.  A pre-migration gunicorn without
  # scope state is never killed heuristically; a live port below fails loudly and
  # requires one explicit operator cleanup.
  if [ -f "$NER_SCOPE" ]; then
    bash "$(dirname "$0")/stop_ner.sh"
  fi
  sleep 2
  # If :5051 STILL answers, a foreign gunicorn owns the port — adopting it would
  # neutralize the fingerprint (this exact gap let a bootstrap-era leftover serve
  # production CI runs). Fail loudly instead.
  if curl -fsS -m 5 -X POST "$NER_URL" -H 'Content-Type: application/json' \
        -d '{"text":"בדיקה","lang":"he"}' >/dev/null 2>&1; then
    echo "::error::a gunicorn we cannot kill (another user?) still owns :5051 — refusing to adopt it; clean it up manually"
    exit 1
  fi
fi
if [ "$STACK_ROLE" != resolver ] && ! curl -fsS -m 5 -X POST "$NER_URL" -H 'Content-Type: application/json' \
      -d '{"text":"בדיקה","lang":"he"}' >/dev/null 2>&1; then
  # A same-fingerprint master may have become unhealthy while descendants still
  # exist. Reap its verified group before starting a replacement; never overlap
  # two model stacks merely because the HTTP probe is down.
  if [ -f "$NER_SCOPE" ]; then
    bash "$(dirname "$0")/stop_ner.sh"
  fi
  # A new session makes the master and all workers one owned process group.
  pushd "$GPU/app" >/dev/null
  # Console-script shebangs inside a moved venv contain the builder path. Invoke
  # gunicorn as a module through the verified interpreter so the bundle is truly
  # relocatable rather than relying on a textual shebang rewrite.
  setsid env APP_CONFIG=local_config.py "$GPU/.venv/bin/python" -m gunicorn.app.wsgiapp \
      -w "$GUNICORN_WORKERS" --timeout 600 \
      --pid "$NER_PIDFILE" -b 127.0.0.1:5051 'app:create_app()' \
      >"$CACHE/gunicorn.log" 2>&1 &
  NER_LAUNCH_PID=$!
  popd >/dev/null
  for _ in $(seq 1 50); do [ -s "$NER_PIDFILE" ] && break; sleep 0.2; done
  [ -s "$NER_PIDFILE" ] || { echo "::error::gunicorn did not create its pidfile"; exit 1; }
  NER_MASTER_PID="$(cat "$NER_PIDFILE")"
  [[ "$NER_MASTER_PID" =~ ^[1-9][0-9]*$ ]]
  [ "$NER_MASTER_PID" = "$NER_LAUNCH_PID" ] || { echo "::error::gunicorn master pid differs from session leader"; exit 1; }
  python3 "$PROCESS_SCOPE" record --state "$NER_SCOPE" --pid "$NER_MASTER_PID" \
      --kind ner-gunicorn --expect 'app:create_app()' --mode "$NER_SCOPE_MODE"
  for _ in $(seq 1 60); do
    curl -fsS -m 5 -X POST "$NER_URL" -H 'Content-Type: application/json' \
      -d '{"text":"בדיקה","lang":"he"}' >/dev/null 2>&1 && break
    sleep 5
  done
  curl -fsS -m 5 -X POST "$NER_URL" -H 'Content-Type: application/json' \
    -d '{"text":"בדיקה","lang":"he"}' >/dev/null || { echo "::error::NER did not come up"; tail -50 "$CACHE/gunicorn.log"; exit 1; }
elif [ "$STACK_ROLE" != resolver ]; then
  python3 "$PROCESS_SCOPE" check --state "$NER_SCOPE" --expect 'app:create_app()' || {
    echo "::error::healthy :5051 service lacks valid process ownership state; refusing to adopt it"
    exit 1
  }
fi
if [ "$STACK_ROLE" != resolver ]; then
  printf '%s' "$NER_ID" > "$NER_MARKER"
fi

# ── 6. Engine fingerprint (consumed by relink.yml → incremental --engine-fingerprint) ─
# Everything that shapes linker OUTPUT: the two upstream checkouts, the model wheels,
# the Mongo dump identity (Refs are resolved against it), and the engine source itself
# (link_books.py + the artifact contract). relink.yml appends the policy flags.
{
  echo "sefaria=$(git -C "$SEF" rev-parse HEAD)"
  echo "sefaria_patch=$(sha256sum "$PATCH" | cut -c1-16)"
  echo "gpu-server=$(git -C "$GPU" rev-parse HEAD)"
  echo "python_runtime=$PYTHON_RUNTIME_ID"
  echo "dump=$DUMP_CONTENT_ID"
  echo "engine_src=$(cat \
    "${LINKER_REPO:-$PWD}/src/link_books.py" \
    "${LINKER_REPO:-$PWD}/src/linker_artifact.py" \
    "${LINKER_REPO:-$PWD}/src/line_baseline.py" \
    "${LINKER_REPO:-$PWD}/src/incremental.py" \
    "${LINKER_REPO:-$PWD}/src/ner_handoff.py" \
    "${LINKER_REPO:-$PWD}/src/precompute_ner.py" \
    | sha256sum | cut -c1-16)"
  for name in he_ref_ner he_subref_ner; do
    whl=$(find "$MODELS" -name "${name}-*.whl" | head -1)
    echo "${name}=$(sha256sum "$whl" | cut -c1-16)"
  done
} > "$STACK/engine_fingerprint.txt"

echo "stack up: role=$STACK_ROLE Sefaria venv=$SEF/.venv NER=$([ "$STACK_ROLE" = resolver ] && echo disabled || echo "$NER_URL")"
echo "engine fingerprint:"; cat "$STACK/engine_fingerprint.txt"
