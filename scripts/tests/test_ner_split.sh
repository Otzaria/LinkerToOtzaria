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
assert "ner_checkpoint_source_run_id:" in workflow_text
assert "ner_checkpoint_source_engine_fingerprint:" in workflow_text
assert "--checkpoint-engine-fingerprint" in workflow_text
assert "flock -w 3600 9" in workflow_text
assert "Restore exact prior-attempt NER checkpoint" in workflow_text
assert 'PYTHONPATH="$SEF_PROJECT${PYTHONPATH:+:$PYTHONPATH}"' in workflow_text
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
from precompute_ner import (
    BATCH_LINES, _checkpoint_document, _largest_books_first, _prepare_root,
    _produce_book, _try_claim,
)
import precompute_ner

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

# A fresh claim with no heartbeat is the mkdir/touch race window, not a stale claim.
claim_root = tmp / "claims-race"
(claim_root / "claims" / cid).mkdir(parents=True)
(claim_root / "done").mkdir()
assert not _try_claim(claim_root, cid)
old = 1
import os
os.utime(claim_root / "claims" / cid, (old, old))
assert _try_claim(claim_root, cid)
assert (claim_root / "claims" / cid / "heartbeat").is_file()

# A blocked GPU call cannot let the claim age stale: the independent lease heartbeat
# advances even while the owning worker's main thread is not making batch progress.
import threading
import time
claim_hb = claim_root / "claims" / cid / "heartbeat"
worker_lease_hb = claim_root / "worker-heartbeat"
before = claim_hb.stat().st_mtime_ns
precompute_ner.CLAIM_HEARTBEAT_SEC = 0.01
stop_lease = threading.Event()
lease_thread = threading.Thread(
    target=precompute_ner._claim_heartbeat_loop,
    args=(claim_root, cid, worker_lease_hb, stop_lease),
)
lease_thread.start()
time.sleep(0.04)
stop_lease.set()
lease_thread.join()
assert claim_hb.stat().st_mtime_ns > before
assert worker_lease_hb.is_file()

# Longest books are attempted first, deterministically, so the bounded GPU window
# cannot strand the one large book after all short work.
import sqlite3
db = sqlite3.connect(":memory:")
db.execute("CREATE TABLE lines_snapshot(source_name TEXT, canonical_he_title TEXT, line_index INTEGER, content TEXT)")
large = BookKey("Source", "גדול")
small = BookKey("Source", "קטן")
db.executemany(
    "INSERT INTO lines_snapshot VALUES(?,?,?,?)",
    [(small.source_name, small.canonical_he_title, 0, "א")] +
    [(large.source_name, large.canonical_he_title, index, "א") for index in range(3)],
)
assert _largest_books_first(db, [small, large]) == [large, small]
db.close()

# A committed partial batch survives checkpoint preparation; its descriptor is
# content-verified and remains available to resume inside the large book.
partial_root = tmp / "partial-resume"
partial_snapshot = tmp / "partial-snapshot.db"
partial_snapshot.write_bytes(b"partial-snapshot")
partial_args = argparse.Namespace(
    output=str(partial_root), snapshot=str(partial_snapshot),
    relink_request_id="4" * 64,
    engine_fingerprint="engine=x;policy=drop;bavli=0",
)
partial_hashes = {(book.source_name, book.canonical_he_title): "5" * 16}
write_json_atomic(
    partial_root / "checkpoint.json",
    _checkpoint_document(partial_args, [book], partial_hashes),
)
for name in ("ner-data", "done", "failed", "partial"):
    (partial_root / name).mkdir(parents=True, exist_ok=True)
partial_dir = partial_root / "partial" / cid
partial_dir.mkdir()
batch_path = partial_dir / "000000000000.json"
batch_size, batch_sha = write_json_atomic(batch_path, {
    "schema_version": 1,
    "book": book.to_dict(),
    "batch_start": 0,
    "lines": [],
})
write_json_atomic(partial_dir / "partial_manifest.json", {
    "schema_version": 1,
    "book": book.to_dict(),
    "source_book_hash": "5" * 16,
    "line_count": 1,
    "batches": [{
        "batch_start": 0,
        "path": f"partial/{cid}/000000000000.json",
        "size": batch_size,
        "sha256": batch_sha,
    }],
})
_prepare_root(partial_args, [book], partial_hashes)
assert batch_path.is_file()
(partial_root / "failed" / cid).write_text("{}\n")
(partial_root / "done" / cid).touch()
_prepare_root(partial_args, [book], partial_hashes)
assert batch_path.is_file()
assert not (partial_root / "failed" / cid).exists()
assert not (partial_root / "done" / cid).exists()

# A cross-run checkpoint is accepted only for the exact operator-attested old engine
# fingerprint, then atomically rebound to the current engine after validation.
migration_root = tmp / "checkpoint-migration"
migration_snapshot = tmp / "migration-snapshot.db"
migration_snapshot.write_bytes(b"migration-snapshot")
migration_args = argparse.Namespace(
    output=str(migration_root), snapshot=str(migration_snapshot),
    relink_request_id="7" * 64,
    engine_fingerprint="engine=new;policy=drop;bavli=0",
    checkpoint_engine_fingerprint="engine=old;policy=drop;bavli=0",
)
migration_hashes = {(book.source_name, book.canonical_he_title): "8" * 16}
old_args = argparse.Namespace(
    **{
        **vars(migration_args),
        "engine_fingerprint": migration_args.checkpoint_engine_fingerprint,
    }
)
write_json_atomic(
    migration_root / "checkpoint.json",
    _checkpoint_document(old_args, [book], migration_hashes),
)
_prepare_root(migration_args, [book], migration_hashes)
assert __import__("json").loads(
    (migration_root / "checkpoint.json").read_text()
) == _checkpoint_document(migration_args, [book], migration_hashes)

# Resume inside a large book: committed batches are validated against the current
# normalized lines and only the missing batch reaches the GPU transport.
resume_root = tmp / "batch-resume"
for name in ("claims", "done", "failed", "partial", "worker-heartbeats", "ner-data"):
    (resume_root / name).mkdir(parents=True, exist_ok=True)
resume_book = BookKey("Source", "ספר גדול")
resume_cid = claim_id(resume_book)
resume_db = sqlite3.connect(tmp / "resume.db")
resume_db.execute(
    "CREATE TABLE lines_snapshot(source_name TEXT, canonical_he_title TEXT, line_index INTEGER, content TEXT)"
)
resume_db.executemany(
    "INSERT INTO lines_snapshot VALUES(?,?,?,?)",
    [(resume_book.source_name, resume_book.canonical_he_title, index, f"טקסט {index}")
     for index in range(BATCH_LINES + 5)],
)
resume_db.commit()
resume_partial = resume_root / "partial" / resume_cid
resume_partial.mkdir()
first_lines = [{
    "line_index": index,
    "normalized_sha256": __import__("hashlib").sha256(f"טקסט {index}".encode()).hexdigest(),
    "result": {"entities": []},
} for index in range(BATCH_LINES)]
first_path = resume_partial / "000000000000.json"
first_size, first_sha = write_json_atomic(first_path, {
    "schema_version": 1,
    "book": resume_book.to_dict(),
    "batch_start": 0,
    "lines": first_lines,
})
write_json_atomic(resume_partial / "partial_manifest.json", {
    "schema_version": 1,
    "book": resume_book.to_dict(),
    "source_book_hash": "6" * 16,
    "line_count": BATCH_LINES + 5,
    "batches": [{
        "batch_start": 0,
        "path": f"partial/{resume_cid}/000000000000.json",
        "size": first_size,
        "sha256": first_sha,
    }],
})
(resume_root / "claims" / resume_cid).mkdir()
(resume_root / "claims" / resume_cid / "heartbeat").touch()
worker_hb = resume_root / "worker-heartbeats" / "test"
worker_hb.touch()
calls = []
precompute_ner._post_bulk = lambda _url, texts: (
    calls.append(list(texts)) or [{"entities": []} for _ in texts]
)
class IdentityNormalizer:
    def _normalize_input(self, values):
        return values
_produce_book(
    argparse.Namespace(ner_url="http://unused", worker_label="test"),
    IdentityNormalizer(), resume_db, resume_book, "6" * 16, resume_root, worker_hb,
)
resume_db.close()
assert len(calls) == 1 and len(calls[0]) == 5
final_manifest = __import__("json").loads(
    (resume_root / "ner-data" / resume_cid / "book_manifest.json").read_text()
)
assert [item["batch_start"] for item in final_manifest["batches"]] == [0, BATCH_LINES]
assert all(item["path"].startswith(f"ner-data/{resume_cid}/") for item in final_manifest["batches"])
assert (resume_root / "done" / resume_cid).is_file()

# Books with no eligible lines still produce a valid empty book manifest.
empty_root = tmp / "empty-book"
for name in ("claims", "done", "failed", "partial", "worker-heartbeats", "ner-data"):
    (empty_root / name).mkdir(parents=True, exist_ok=True)
empty_book = BookKey("Source", "ספר ריק")
empty_cid = claim_id(empty_book)
empty_db = sqlite3.connect(tmp / "empty.db")
empty_db.execute(
    "CREATE TABLE lines_snapshot(source_name TEXT, canonical_he_title TEXT, line_index INTEGER, content TEXT)"
)
empty_db.execute(
    "INSERT INTO lines_snapshot VALUES(?,?,?,?)",
    (empty_book.source_name, empty_book.canonical_he_title, 0, "א"),
)
empty_db.commit()
(empty_root / "claims" / empty_cid).mkdir()
(empty_root / "claims" / empty_cid / "heartbeat").touch()
empty_hb = empty_root / "worker-heartbeats" / "test"
empty_hb.touch()
_produce_book(
    argparse.Namespace(ner_url="http://unused", worker_label="test"),
    IdentityNormalizer(), empty_db, empty_book, "9" * 16, empty_root, empty_hb,
)
empty_db.close()
empty_manifest = __import__("json").loads(
    (empty_root / "ner-data" / empty_cid / "book_manifest.json").read_text()
)
assert empty_manifest["eligible_line_count"] == 0
assert empty_manifest["batches"] == []
PY
bash "$ROOT/ci/pack_ner_checkpoint.sh" "$TMP/checkpoint" "$TMP/checkpoint.tar.zst"
python3 "$ROOT/ci/unpack_ner_checkpoint.py" \
  "$TMP/checkpoint.tar.zst" "$TMP/checkpoint.tar.zst.sha256" "$TMP/checkpoint-unpacked"
test -d "$TMP/checkpoint-unpacked/failed"
test -d "$TMP/checkpoint-unpacked/partial"
echo "ok   resumable checkpoint is checksum-gated and failed books are retried"
