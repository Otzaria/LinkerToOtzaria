#!/usr/bin/env bash
# Run one raw-NER shard on a CPU host. No Mongo and no resolver are started.
set -euo pipefail

if [ "$#" -ne 9 ]; then
  echo "usage: $0 INPUT_DIR WORK_DIR OUTPUT_DIR SHARD_INDEX SEFARIA_DIR GPU_DIR SEFARIA_PYTHON GPU_PYTHON MODELS_DIR" >&2
  exit 2
fi
INPUT_DIR=$1
WORK_DIR=$2
OUTPUT_DIR=$3
SHARD_INDEX=$4
SEFARIA_DIR=$5
GPU_DIR=$6
SEFARIA_PYTHON=$7
GPU_PYTHON=$8
MODELS_DIR=$9
WORKERS=${NER_WORKERS:-1}
BATCH_LINES=${LINKER_BATCH_LINES:-75}
LEASE=${HOST_LEASE_PATH:-/run/lock/otzaria/host-heavy.lock}

mkdir -p "$WORK_DIR" "$OUTPUT_DIR"
exec 9>>"$LEASE"
flock -n 9 || {
  echo "host lease is busy: $LEASE" >&2
  exit 1
}

SNAPSHOT="$WORK_DIR/lines_snapshot.db"
if [ ! -f "$SNAPSHOT" ]; then
  unzstd -f "$INPUT_DIR/lines_snapshot_context_v2.db.zst" -o "$SNAPSHOT"
fi
if [ ! -d "$WORK_DIR/LinkerToOtzaria/src" ]; then
  tar --use-compress-program=unzstd -xf "$INPUT_DIR/linker-source.tar.zst" -C "$WORK_DIR"
fi
LINKER_DIR="$WORK_DIR/LinkerToOtzaria"

if [ -z "$SEFARIA_DIR" ]; then
  tar --use-compress-program=unzstd -xf "$INPUT_DIR/sefaria-source.tar.zst" -C "$WORK_DIR"
  SEFARIA_DIR="$WORK_DIR/Sefaria-Project"
fi
if [ -z "$GPU_DIR" ]; then
  tar --use-compress-program=unzstd -xf "$INPUT_DIR/gpu-source.tar.zst" -C "$WORK_DIR"
  GPU_DIR="$WORK_DIR/gpu-server"
fi
if [ -z "$MODELS_DIR" ]; then
  MODELS_DIR="$WORK_DIR/models"
  mkdir -p "$MODELS_DIR"
  tar -xzf "$INPUT_DIR/linker_models.tar.gz" -C "$MODELS_DIR"
  for name in he_ref_ner he_subref_ner; do
    mkdir -p "$MODELS_DIR/${name}_pkg"
    unzip -qo "$MODELS_DIR"/"${name}"-*.whl -d "$MODELS_DIR/${name}_pkg"
  done
fi
REF_MODEL=$(find "$MODELS_DIR/he_ref_ner_pkg/he_ref_ner" -mindepth 1 -maxdepth 1 -type d -name 'he_ref_ner-*' | head -1)
SUBREF_MODEL=$(find "$MODELS_DIR/he_subref_ner_pkg/he_subref_ner" -mindepth 1 -maxdepth 1 -type d -name 'he_subref_ner-*' | head -1)
[ -n "$REF_MODEL" ] && [ -n "$SUBREF_MODEL" ] || {
  echo "model directories are missing" >&2
  exit 1
}
cat > "$GPU_DIR/app/local_config.py" <<EOF
MODEL_PATHS = [
    {"arch": "spacy", "lang": "he", "path": "$REF_MODEL", "type": "named_entity"},
    {"arch": "spacy", "lang": "he", "path": "$SUBREF_MODEL", "type": "ref_part"},
]
EOF

if curl -fsS -m 3 -X POST http://127.0.0.1:5051/recognize-entities \
    -H 'Content-Type: application/json' -d '{"text":"בדיקה","lang":"he"}' >/dev/null 2>&1; then
  echo "port 5051 is already owned; refusing to adopt an unrelated NER service" >&2
  exit 1
fi
pushd "$GPU_DIR/app" >/dev/null
setsid env APP_CONFIG=local_config.py "$GPU_PYTHON" -m gunicorn.app.wsgiapp \
  -w "$WORKERS" --timeout 600 -b 127.0.0.1:5051 'app:create_app()' \
  >"$OUTPUT_DIR/ner-service.log" 2>&1 &
NER_PID=$!
popd >/dev/null
cleanup() {
  kill -TERM -- "-$NER_PID" 2>/dev/null || true
  for _ in $(seq 1 40); do kill -0 "$NER_PID" 2>/dev/null || break; sleep 0.25; done
  kill -KILL -- "-$NER_PID" 2>/dev/null || true
  wait "$NER_PID" 2>/dev/null || true
}
trap cleanup EXIT
for _ in $(seq 1 120); do
  curl -fsS -m 5 -X POST http://127.0.0.1:5051/recognize-entities \
    -H 'Content-Type: application/json' -d '{"text":"בדיקה","lang":"he"}' >/dev/null 2>&1 && break
  sleep 5
done
curl -fsS -m 5 -X POST http://127.0.0.1:5051/recognize-entities \
  -H 'Content-Type: application/json' -d '{"text":"בדיקה","lang":"he"}' >/dev/null

readarray -t IDENTITY < <("$SEFARIA_PYTHON" - "$INPUT_DIR/parallel_ner_input_manifest.json" <<'PY'
import json,sys
value=json.load(open(sys.argv[1],encoding="utf-8"))
print(value["relink_request_id"])
print(value["engine_fingerprint"])
PY
)
REQUEST_ID=${IDENTITY[0]}
ENGINE_FINGERPRINT=${IDENTITY[1]}
PYTHONPATH="$SEFARIA_DIR" \
LINKER_BATCH_LINES="$BATCH_LINES" \
LINKER_NER_MAX_WAIT_SEC=300 \
"$SEFARIA_PYTHON" "$LINKER_DIR/src/precompute_ner.py" \
  --snapshot "$SNAPSHOT" \
  --plan "$INPUT_DIR/shard-${SHARD_INDEX}.json" \
  --output "$OUTPUT_DIR/raw-ner-shard-${SHARD_INDEX}" \
  --relink-request-id "$REQUEST_ID" \
  --engine-fingerprint "$ENGINE_FINGERPRINT" \
  --workers "$WORKERS"
