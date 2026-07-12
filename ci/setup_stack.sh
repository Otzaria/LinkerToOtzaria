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

url_key() { printf '%s' "$1" | sha256sum | cut -c1-12; }

# ── 1. Sefaria-Project + gpu-server checkouts ────────────────────────────────
# Optional pins via SEFARIA_COMMIT / GPU_SERVER_COMMIT; unpinned drift is still safe —
# the fingerprint below changes with HEAD and triggers a full relink.
[ -d "$SEF" ] || git clone https://github.com/Sefaria/Sefaria-Project "$SEF"
[ -d "$GPU" ] || git clone https://github.com/Sefaria/gpu-server "$GPU"
[ -z "${SEFARIA_COMMIT:-}" ] || git -C "$SEF" -c advice.detachedHead=false checkout "$SEFARIA_COMMIT"
[ -z "${GPU_SERVER_COMMIT:-}" ] || git -C "$GPU" -c advice.detachedHead=false checkout "$GPU_SERVER_COMMIT"

# ── 2. Python venv (numpy<2 — thinc/spaCy binary compat, learned in the POC) ─
if [ ! -x "$SEF/.venv/bin/python" ]; then
  python3 -m venv "$SEF/.venv"
  "$SEF/.venv/bin/pip" install --upgrade pip
  "$SEF/.venv/bin/pip" install -r "$SEF/requirements.txt"
  "$SEF/.venv/bin/pip" install "numpy<2"
fi
# local_settings: point Django at the local Mongo; secrets stay empty (offline linking).
if [ ! -f "$SEF/sefaria/local_settings.py" ]; then
  cp "$SEF/sefaria/local_settings_example.py" "$SEF/sefaria/local_settings.py"
fi

# ── 3. MongoDB + Sefaria dump (cached per dump URL) ─────────────────────────
# The marker embeds the URL hash: pointing LINKER_DUMP_URL at a new dump re-restores.
: "${LINKER_DUMP_URL:?set LINKER_DUMP_URL to the Sefaria mongo dump archive}"
DUMP_MARKER="$CACHE/.dump-restored-$(url_key "$LINKER_DUMP_URL")"
pgrep -x mongod >/dev/null || (mkdir -p "$CACHE/mongo-data" && mongod --dbpath "$CACHE/mongo-data" --fork --logpath "$CACHE/mongod.log")
if [ ! -f "$DUMP_MARKER" ]; then
  curl -fsSL -o "$CACHE/dump.tar.gz" "$LINKER_DUMP_URL"
  tar -xzf "$CACHE/dump.tar.gz" -C "$CACHE"
  mongorestore --drop "$CACHE/dump"
  rm -f "$CACHE"/.dump-restored-*
  touch "$DUMP_MARKER"
fi

# ── 4. NER model wheels (cached per models URL) ──────────────────────────────
# LINKER_MODELS_URL → tar/tgz containing he_ref_ner-*.whl + he_subref_ner-*.whl
# (HuggingFace is not reachable from every runner; host the two wheels yourself).
: "${LINKER_MODELS_URL:?set LINKER_MODELS_URL to an archive with the two NER model wheels}"
MODELS="$CACHE/models-$(url_key "$LINKER_MODELS_URL")"
if [ ! -f "$MODELS/.ready" ]; then
  rm -rf "$MODELS" && mkdir -p "$MODELS"
  curl -fsSL -o "$MODELS/models.archive" "$LINKER_MODELS_URL"
  tar -xf "$MODELS/models.archive" -C "$MODELS"
  for name in he_ref_ner he_subref_ner; do
    whl=$(find "$MODELS" -name "${name}-*.whl" | head -1)
    [ -n "$whl" ] || { echo "::error::${name} wheel missing from LINKER_MODELS_URL archive"; exit 1; }
    unzip -qo "$whl" -d "$MODELS/${name}_pkg"
  done
  touch "$MODELS/.ready"
fi
model_path() { find "$MODELS/$1_pkg/$1" -maxdepth 1 -mindepth 1 -type d -name "$1-*" | head -1; }
REF_MODEL=$(model_path he_ref_ner)
SUBREF_MODEL=$(model_path he_subref_ner)
[ -n "$REF_MODEL" ] && [ -n "$SUBREF_MODEL" ] || { echo "::error::model dirs not found after unzip"; exit 1; }

# ── 5. gpu-server venv + generated config + gunicorn on :5051 ────────────────
# app/local_config.py is generated here (it is machine-specific, never committed).
if [ ! -x "$GPU/.venv/bin/gunicorn" ]; then
  python3 -m venv "$GPU/.venv"
  "$GPU/.venv/bin/pip" install --upgrade pip
  "$GPU/.venv/bin/pip" install -r "$GPU/requirements.txt"
fi
cat > "$GPU/app/local_config.py" <<EOF
MODEL_PATHS = [
    {"arch": "spacy", "lang": "he", "path": "$REF_MODEL", "type": "named_entity"},
    {"arch": "spacy", "lang": "he", "path": "$SUBREF_MODEL", "type": "ref_part"},
]
EOF
if ! curl -fsS -m 5 -X POST "$NER_URL" -H 'Content-Type: application/json' \
      -d '{"text":"בדיקה","lang":"he"}' >/dev/null 2>&1; then
  ( cd "$GPU/app" && APP_CONFIG=local_config.py nohup "$GPU/.venv/bin/gunicorn" -w 3 --timeout 600 \
      -b 127.0.0.1:5051 'app:create_app()' >"$CACHE/gunicorn.log" 2>&1 & )
  for _ in $(seq 1 60); do
    curl -fsS -m 5 -X POST "$NER_URL" -H 'Content-Type: application/json' \
      -d '{"text":"בדיקה","lang":"he"}' >/dev/null 2>&1 && break
    sleep 5
  done
  curl -fsS -m 5 -X POST "$NER_URL" -H 'Content-Type: application/json' \
    -d '{"text":"בדיקה","lang":"he"}' >/dev/null || { echo "::error::NER did not come up"; tail -50 "$CACHE/gunicorn.log"; exit 1; }
fi

# ── 6. Engine fingerprint (consumed by relink.yml → incremental --engine-fingerprint) ─
{
  echo "sefaria=$(git -C "$SEF" rev-parse HEAD)"
  echo "gpu-server=$(git -C "$GPU" rev-parse HEAD)"
  for name in he_ref_ner he_subref_ner; do
    whl=$(find "$MODELS" -name "${name}-*.whl" | head -1)
    echo "${name}=$(sha256sum "$whl" | cut -c1-16)"
  done
} > "$STACK/engine_fingerprint.txt"

echo "stack up: Sefaria venv=$SEF/.venv, NER=$NER_URL"
echo "engine fingerprint:"; cat "$STACK/engine_fingerprint.txt"
