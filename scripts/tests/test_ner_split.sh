#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export PYTHONPATH="$ROOT/src"

python3 - "$TMP" "$ROOT/.github/workflows/relink.yml" <<'PY'
import json
import sys
from pathlib import Path

import yaml
from linker_artifact import BookKey
from ner_handoff import (
    NerBundle, canonical_json, sha256_bytes, validate_ner_result, write_json_atomic,
)

root = Path(sys.argv[1]) / "bundle"
cid = "a" * 40
book = BookKey("Source", "ספר")
original = "אב־גד"
normalized = "אב-גד"
result = {
    "entities": [{
        "label": "מקור",
        "range": [0, 5],
        "parts": [{"label": "BOOK", "range": [0, 2]}],
    }],
}
batch = {
    "schema_version": 1,
    "book": book.to_dict(),
    "batch_start": 0,
    "lines": [{
        "line_index": 7,
        "normalized_sha256": sha256_bytes(normalized.encode()),
        "result": result,
    }],
}
batch_path = root / "ner-data" / cid / "000000000000.json"
batch_size, batch_sha = write_json_atomic(batch_path, batch)
book_manifest = {
    "schema_version": 1,
    "book": book.to_dict(),
    "source_book_hash": "1" * 16,
    "line_count": 1,
    "eligible_line_count": 1,
    "batches": [{
        "batch_start": 0,
        "path": f"ner-data/{cid}/000000000000.json",
        "size": batch_size,
        "sha256": batch_sha,
    }],
}
book_path = root / "ner-data" / cid / "book_manifest.json"
book_size, book_sha = write_json_atomic(book_path, book_manifest)
request = "2" * 64
snapshot = "3" * 64
fingerprint = "engine=x;policy=drop;bavli=0"
write_json_atomic(root / "ner_manifest.json", {
    "schema_version": 1,
    "relink_request_id": request,
    "snapshot_sha256": snapshot,
    "engine_fingerprint": fingerprint,
    "batch_lines": 25,
    "books": [{
        "book": book.to_dict(),
        "manifest_path": f"ner-data/{cid}/book_manifest.json",
        "size": book_size,
        "sha256": book_sha,
    }],
})

class FakeRecognizer:
    normalizer = object()
    def _normalize_input(self, inputs):
        assert inputs == [original]
        return [normalized]
    def _parse_recognize_response(self, text, value):
        assert text == normalized and value == result
        return ["raw-ref"], []

class FakeLinker:
    def __init__(self):
        self._ner = FakeRecognizer()
    def get_ner(self):
        return self._ner
    def bulk_link(self, inputs, type_filter):
        assert inputs == [original] and type_filter == "citation"
        assert self._ner.bulk_recognize(inputs) == [["raw-ref"]]
        return ["resolved-doc"]

bundle = NerBundle(
    root,
    request_id=request,
    snapshot_sha256=snapshot,
    engine_fingerprint=fingerprint,
    changed_books=[book.to_dict()],
    expected_book_hashes={(book.source_name, book.canonical_he_title): "1" * 16},
)
assert bundle.resolve_batch(FakeLinker(), book, [(7, original)], 0) == ["resolved-doc"]

# Adversarial schema/range cases must fail closed.
for bad in (
    {"entities": [{"label": "x", "range": [0, True]}]},
    {"entities": [{"label": "x", "range": [-1, 0]}]},
    {"entities": [{"label": "x", "range": [0, 99]}]},
    {"entities": [{"label": "x", "range": [0, 1], "extra": 1}]},
):
    try:
        validate_ner_result(bad, "abc", "bad")
    except RuntimeError:
        pass
    else:
        raise AssertionError(f"accepted malformed NER result: {bad}")

# A single byte of shard drift is caught before parsing/resolution.
raw = batch_path.read_bytes()
batch_path.write_bytes(raw[:-2] + b" \\n")
try:
    NerBundle(
        root,
        request_id=request,
        snapshot_sha256=snapshot,
        engine_fingerprint=fingerprint,
        changed_books=[book.to_dict()],
    )
except RuntimeError:
    pass
else:
    raise AssertionError("accepted a tampered NER shard")
batch_path.write_bytes(raw)

# Workflow inputs may enter shell only through env, never expression interpolation.
workflow = yaml.load(Path(sys.argv[2]).read_text(), Loader=yaml.BaseLoader)
def walk(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "run" and "${{ inputs." in item:
                raise AssertionError("workflow input is interpolated directly into a run body")
            walk(item)
    elif isinstance(value, list):
        for item in value:
            walk(item)
walk(workflow)
jobs = workflow["jobs"]
assert jobs["resolve"]["needs"] == "relink"
assert "LINKER_STACK_ROLE: resolver" in Path(sys.argv[2]).read_text()
workflow_text = Path(sys.argv[2]).read_text()
assert "raw-ner-handoff-${RELINK_REQUEST_ID}-${GITHUB_RUN_ATTEMPT}" in workflow_text
assert "--deadline-seconds 3600" in workflow_text
assert "compression-level: 0" in workflow_text
assert "raw_ner_source_run_id:" in workflow_text
assert "flock -w 3600 9" in workflow_text
assert "Restore exact prior-attempt NER checkpoint" in workflow_text
producer = (Path(sys.argv[2]).parents[2] / "src/precompute_ner.py").read_text()
assert "from sefaria.model" not in producer
assert "django.setup" not in producer
assert 'from sefaria.helper.normalization import NormalizerComposer' in producer
print("ok   raw-NER replay is exact, content-addressed, and fail-closed")
PY

# Archive extraction rejects digest drift and preserves only contract roots.
bash "$ROOT/ci/pack_ner_handoff.sh" "$TMP/bundle" "$TMP/ner.tar.zst"
mkdir -p "$TMP/unpack-parent"
python3 "$ROOT/ci/unpack_ner_handoff.py" \
  "$TMP/ner.tar.zst" "$TMP/ner.tar.zst.sha256" "$TMP/unpack-parent/handoff"
test -f "$TMP/unpack-parent/handoff/ner_manifest.json"
test -d "$TMP/unpack-parent/handoff/ner-data"
printf '%064d\n' 0 > "$TMP/bad.sha"
! python3 "$ROOT/ci/unpack_ner_handoff.py" \
  "$TMP/ner.tar.zst" "$TMP/bad.sha" "$TMP/unpack-parent/bad" >/dev/null 2>&1
echo "ok   raw-NER archive is deterministic and checksum-gated"

# A failed book must retain its failed marker so resume retries rather than trusting done.
python3 - "$TMP" <<'PY'
import argparse
import shutil
import sys
from pathlib import Path

from linker_artifact import BookKey
from link_books import claim_id
from precompute_ner import _checkpoint_document, _prepare_root

tmp = Path(sys.argv[1])
root = tmp / "checkpoint"
shutil.copytree(tmp / "bundle", root)
snapshot = tmp / "snapshot.db"
snapshot.write_bytes(b"snapshot")
book = BookKey("Source", "ספר")
args = argparse.Namespace(
    output=str(root), snapshot=str(snapshot), relink_request_id="2" * 64,
    engine_fingerprint="engine=x;policy=drop;bavli=0",
)
hashes = {(book.source_name, book.canonical_he_title): "1" * 16}
from ner_handoff import write_json_atomic
write_json_atomic(root / "checkpoint.json", _checkpoint_document(args, [book], hashes))
for name in ("done", "failed"):
    (root / name).mkdir(exist_ok=True)
cid = claim_id(book)
(root / "ner-data" / cid).mkdir(exist_ok=True)
(root / "done" / cid).touch()
(root / "failed" / cid).write_text("{}\n")
prepared = _prepare_root(args, [book], hashes)
assert not (prepared / "done" / cid).exists()
assert not (prepared / "failed" / cid).exists()
assert not (prepared / "ner-data" / cid).exists()
PY
bash "$ROOT/ci/pack_ner_checkpoint.sh" "$TMP/checkpoint" "$TMP/checkpoint.tar.zst"
python3 "$ROOT/ci/unpack_ner_checkpoint.py" \
  "$TMP/checkpoint.tar.zst" "$TMP/checkpoint.tar.zst.sha256" "$TMP/checkpoint-unpacked"
test -d "$TMP/checkpoint-unpacked/failed"
echo "ok   resumable checkpoint is checksum-gated and failed books are retried"
