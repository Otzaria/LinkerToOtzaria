#!/usr/bin/env bash
# Deterministically package the GPU-only raw-NER contract for the CPU resolver.
set -euo pipefail

[ "$#" -eq 2 ] || { echo "usage: $0 NER_BUNDLE_DIR OUTPUT.tar.zst" >&2; exit 2; }
ROOT=$1
OUT=$2
test -f "$ROOT/ner_manifest.json"
test -d "$ROOT/ner-data"

raw_bytes=$(python3 - "$ROOT/ner_manifest.json" "$ROOT/ner-data" <<'PY'
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
max_raw_bytes=${NER_HANDOFF_MAX_RAW_BYTES:-2147483648}
[[ "$raw_bytes" =~ ^[0-9]+$ && "$max_raw_bytes" =~ ^[1-9][0-9]*$ ]]
if [ "$raw_bytes" -gt "$max_raw_bytes" ]; then
  echo "::error::raw-NER handoff is ${raw_bytes} bytes, above safety cap ${max_raw_bytes}" >&2
  exit 1
fi
python3 "$(dirname "$0")/write_deterministic_tar.py" "$ROOT" ner_manifest.json ner-data \
  | zstd -12 -T0 -o "$OUT" -f
sha256sum "$OUT" | cut -d' ' -f1 > "$OUT.sha256"
packed_bytes=$(python3 -c 'import os,sys; print(os.path.getsize(sys.argv[1]))' "$OUT")
echo "packed raw-NER handoff: raw_bytes=$raw_bytes packed_bytes=$packed_bytes sha256=$(cat "$OUT.sha256")"
