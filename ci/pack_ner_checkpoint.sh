#!/usr/bin/env bash
# Package a resumable, input-bound NER checkpoint after a bounded Kaggle failure.
set -euo pipefail

[ "$#" -eq 2 ] || { echo "usage: $0 CHECKPOINT_DIR OUTPUT.tar.zst" >&2; exit 2; }
ROOT=$1
OUT=$2
test -f "$ROOT/checkpoint.json"
test -d "$ROOT/ner-data"
test -d "$ROOT/done"
test -d "$ROOT/failed"
test -d "$ROOT/partial"

raw_bytes=$(python3 - "$ROOT/checkpoint.json" "$ROOT/ner-data" "$ROOT/done" "$ROOT/failed" "$ROOT/partial" <<'PY'
import pathlib, sys
total = 0
for raw in sys.argv[1:]:
    path = pathlib.Path(raw)
    total += path.stat().st_size if path.is_file() else sum(
        item.stat().st_size for item in path.rglob("*") if item.is_file()
    )
print(total)
PY
)
max_raw_bytes=${NER_CHECKPOINT_MAX_RAW_BYTES:-2147483648}
[[ "$raw_bytes" =~ ^[0-9]+$ && "$max_raw_bytes" =~ ^[1-9][0-9]*$ ]]
if [ "$raw_bytes" -gt "$max_raw_bytes" ]; then
  echo "::error::NER checkpoint is ${raw_bytes} bytes, above safety cap ${max_raw_bytes}" >&2
  exit 1
fi
python3 "$(dirname "$0")/write_deterministic_tar.py" "$ROOT" checkpoint.json ner-data done failed partial \
  | zstd -8 -T0 -o "$OUT" -f
sha256sum "$OUT" | cut -d' ' -f1 > "$OUT.sha256"
packed_bytes=$(python3 -c 'import os,sys; print(os.path.getsize(sys.argv[1]))' "$OUT")
echo "packed resumable NER checkpoint: raw_bytes=$raw_bytes packed_bytes=$packed_bytes sha256=$(cat "$OUT.sha256")"
