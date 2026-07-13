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
mkdir -p "$STACK" "$CACHE"

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
SEF_VENV_ID="$($PYBIN --version 2>&1):$(sha256sum "$SEF/requirements.txt" | cut -c1-12):${SEFARIA_COMMIT:-head}"
if [ ! -x "$SEF/.venv/bin/python" ] || [ "$(cat "$SEF/.venv/.identity" 2>/dev/null)" != "$SEF_VENV_ID" ]; then
  rm -rf "$SEF/.venv"
  "$PYBIN" -m venv "$SEF/.venv"
  "$SEF/.venv/bin/pip" install --upgrade pip
  "$SEF/.venv/bin/pip" install -r "$SEF/requirements.txt"
  "$SEF/.venv/bin/pip" install "numpy<2"
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
DUMP_KEY="$(url_key "$DUMP_REPO@$DUMP_TAG")"
DUMP_MARKER="$CACHE/.dump-restored-$DUMP_KEY"
pgrep -x mongod >/dev/null || (mkdir -p "$CACHE/mongo-data" && mongod --dbpath "$CACHE/mongo-data" --fork --logpath "$CACHE/mongod.log")
if [ ! -f "$DUMP_MARKER" ]; then
  rm -rf "$CACHE/dump-dl" && mkdir -p "$CACHE/dump-dl"
  gh release download "$DUMP_TAG" -R "$DUMP_REPO" -p 'dump.tar.gz.part-*' -p SHA256SUMS -D "$CACHE/dump-dl"
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
  sha256sum "$CACHE/dump-dl/SHA256SUMS" | cut -c1-16 > "$CACHE/.dump-content-id"
  rm -rf "$CACHE/dump-dl" "$CACHE/dump-extract"
  rm -f "$CACHE"/.dump-restored-*
  touch "$DUMP_MARKER"
fi
DUMP_CONTENT_ID="$(cat "$CACHE/.dump-content-id" 2>/dev/null || echo "$DUMP_KEY")"

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
GPU_VENV_ID="$($PYBIN --version 2>&1):$(sha256sum "$GPU/app/requirements.txt" | cut -c1-12):${GPU_SERVER_COMMIT:-head}"
if [ ! -x "$GPU/.venv/bin/gunicorn" ] || [ "$(cat "$GPU/.venv/.identity" 2>/dev/null)" != "$GPU_VENV_ID" ]; then
  rm -rf "$GPU/.venv"
  "$PYBIN" -m venv "$GPU/.venv"
  "$GPU/.venv/bin/pip" install --upgrade pip
  # gunicorn is the serving runtime, not an app dependency — requirements.txt omits it.
  "$GPU/.venv/bin/pip" install -r "$GPU/app/requirements.txt" gunicorn
  printf '%s' "$GPU_VENV_ID" > "$GPU/.venv/.identity"
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
if [ "$(cat "$NER_MARKER" 2>/dev/null)" != "$NER_ID" ]; then
  pkill -f 'gunicorn.*127.0.0.1:5051' 2>/dev/null || true
  sleep 2
  # pkill cannot signal another user's processes and its failure is swallowed above.
  # If :5051 STILL answers, a foreign gunicorn owns the port — adopting it would
  # neutralize the fingerprint (this exact gap let a bootstrap-era leftover serve
  # production CI runs). Fail loudly instead.
  if curl -fsS -m 5 -X POST "$NER_URL" -H 'Content-Type: application/json' \
        -d '{"text":"בדיקה","lang":"he"}' >/dev/null 2>&1; then
    echo "::error::a gunicorn we cannot kill (another user?) still owns :5051 — refusing to adopt it; clean it up manually"
    exit 1
  fi
fi
if ! curl -fsS -m 5 -X POST "$NER_URL" -H 'Content-Type: application/json' \
      -d '{"text":"בדיקה","lang":"he"}' >/dev/null 2>&1; then
  ( cd "$GPU/app" && APP_CONFIG=local_config.py nohup "$GPU/.venv/bin/gunicorn" -w "$GUNICORN_WORKERS" --timeout 600 \
      -b 127.0.0.1:5051 'app:create_app()' >"$CACHE/gunicorn.log" 2>&1 & )
  for _ in $(seq 1 60); do
    curl -fsS -m 5 -X POST "$NER_URL" -H 'Content-Type: application/json' \
      -d '{"text":"בדיקה","lang":"he"}' >/dev/null 2>&1 && break
    sleep 5
  done
  curl -fsS -m 5 -X POST "$NER_URL" -H 'Content-Type: application/json' \
    -d '{"text":"בדיקה","lang":"he"}' >/dev/null || { echo "::error::NER did not come up"; tail -50 "$CACHE/gunicorn.log"; exit 1; }
fi
printf '%s' "$NER_ID" > "$NER_MARKER"

# ── 6. Engine fingerprint (consumed by relink.yml → incremental --engine-fingerprint) ─
# Everything that shapes linker OUTPUT: the two upstream checkouts, the model wheels,
# the Mongo dump identity (Refs are resolved against it), and the engine source itself
# (link_books.py + the artifact contract). relink.yml appends the policy flags.
{
  echo "sefaria=$(git -C "$SEF" rev-parse HEAD)"
  echo "sefaria_patch=$(sha256sum "$PATCH" | cut -c1-16)"
  echo "gpu-server=$(git -C "$GPU" rev-parse HEAD)"
  echo "dump=$DUMP_CONTENT_ID"
  echo "engine_src=$(cat "${LINKER_REPO:-$PWD}/src/link_books.py" "${LINKER_REPO:-$PWD}/src/linker_artifact.py" "${LINKER_REPO:-$PWD}/src/incremental.py" | sha256sum | cut -c1-16)"
  for name in he_ref_ner he_subref_ner; do
    whl=$(find "$MODELS" -name "${name}-*.whl" | head -1)
    echo "${name}=$(sha256sum "$whl" | cut -c1-16)"
  done
} > "$STACK/engine_fingerprint.txt"

echo "stack up: Sefaria venv=$SEF/.venv, NER=$NER_URL"
echo "engine fingerprint:"; cat "$STACK/engine_fingerprint.txt"
