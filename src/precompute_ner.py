"""GPU-only producer for the content-addressed raw-NER handoff."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import re
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from linker_artifact import BookKey  # noqa: E402
from link_books import BATCH_LINES, all_book_keys, book_lines, claim_id  # noqa: E402
from ner_handoff import (  # noqa: E402
    SCHEMA_VERSION,
    load_json_strict,
    safe_relative_path,
    sha256_bytes,
    sha256_file,
    validate_book_key,
    validate_ner_result,
    validate_plan,
    write_json_atomic,
)

CLAIM_STALE_SEC = 600
NER_MAX_WAIT_SEC = int(os.environ.get("LINKER_NER_MAX_WAIT_SEC", "1800"))
HEX64 = re.compile(r"[0-9a-f]{64}")


def _log(label: str, message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} ner-{label} {message}", flush=True)


def _post_bulk(url: str, texts: list[str]) -> list[dict]:
    import requests

    response = requests.post(
        url.rstrip("/") + "/bulk-recognize-entities",
        json={"texts": texts, "lang": "he"},
        timeout=600,
    )
    response.raise_for_status()
    try:
        data = json.loads(response.text, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(f"GPU server returned invalid/duplicate-key JSON: {error}") from error
    if type(data) is not dict or set(data) != {"results"} or type(data["results"]) is not list:
        raise RuntimeError("GPU server returned an invalid bulk response")
    if len(data["results"]) != len(texts):
        raise RuntimeError(
            f"GPU server returned {len(data['results'])} results for {len(texts)} inputs"
        )
    for index, (result, text) in enumerate(zip(data["results"], texts)):
        validate_ner_result(result, text, f"gpu.results[{index}]")
    return data["results"]


def _unique_object(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError(f"duplicate key {key!r}")
        value[key] = item
    return value


def _wait_for_ner(url: str, label: str) -> None:
    import requests

    waited = 0
    while True:
        try:
            response = requests.post(
                url.rstrip("/") + "/recognize-entities",
                json={"text": "בדיקה", "lang": "he"},
                timeout=60,
            )
            response.raise_for_status()
            return
        except Exception:
            if waited >= NER_MAX_WAIT_SEC:
                raise RuntimeError(f"NER unavailable after {waited}s")
            _log(label, "NER unavailable; waiting 15s")
            time.sleep(15)
            waited += 15


def _load_plan(args) -> tuple[dict, list[BookKey], dict[tuple[str, str], str]]:
    plan = load_json_strict(args.plan)
    validated = validate_plan(
        plan,
        request_id=args.relink_request_id,
        snapshot_sha256=sha256_file(args.snapshot),
        engine_fingerprint=args.engine_fingerprint,
    )
    books = [BookKey(item["source_name"], item["canonical_he_title"]) for item in validated["changed"]]
    hashes = {
        (item["source_name"], item["canonical_he_title"]): item["hash"]
        for item in validated["current_books"]
    }
    return validated, books, hashes


def build_producer_normalizer():
    """Construct the pinned training-time normalizer without importing sefaria.model."""
    from sefaria.helper.normalization import NormalizerComposer

    class ProducerNormalizer:
        def __init__(self):
            self.normalizer = NormalizerComposer(
                ["unidecode", "fn-marker", "html", "double-space", "maqaf", "cantillation"]
            )

        def _normalize_input(self, inputs):
            return [self.normalizer.normalize(value) for value in inputs]

    return ProducerNormalizer()


def _checkpoint_document(args, books, hashes) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "relink_request_id": args.relink_request_id,
        "snapshot_sha256": sha256_file(args.snapshot),
        "engine_fingerprint": args.engine_fingerprint,
        "batch_lines": BATCH_LINES,
        "books": [
            {
                "source_name": book.source_name,
                "canonical_he_title": book.canonical_he_title,
                "source_book_hash": hashes[(book.source_name, book.canonical_he_title)],
            }
            for book in books
        ],
    }


def _verify_descriptor(root: Path, descriptor: dict, where: str) -> Path:
    if type(descriptor) is not dict or set(descriptor) != {"batch_start", "path", "size", "sha256"}:
        raise RuntimeError(f"{where}: invalid descriptor shape")
    if type(descriptor["batch_start"]) is not int or descriptor["batch_start"] < 0:
        raise RuntimeError(f"{where}: invalid batch_start")
    relative = safe_relative_path(descriptor["path"], f"{where}.path")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as error:
        raise RuntimeError(f"{where}: path escapes checkpoint") from error
    if (type(descriptor["size"]) is not int or descriptor["size"] <= 0
            or type(descriptor["sha256"]) is not str
            or not HEX64.fullmatch(descriptor["sha256"])):
        raise RuntimeError(f"{where}: invalid content descriptor")
    if (not path.is_file() or path.stat().st_size != descriptor["size"]
            or sha256_file(path) != descriptor["sha256"]):
        raise RuntimeError(f"{where}: checkpoint content digest mismatch")
    return path


def _validate_completed_book(root: Path, book: BookKey, source_hash: str) -> None:
    cid = claim_id(book)
    path = root / "ner-data" / cid / "book_manifest.json"
    value = load_json_strict(path)
    required = {
        "schema_version", "book", "source_book_hash", "line_count",
        "eligible_line_count", "batches",
    }
    if type(value) is not dict or set(value) != required:
        raise RuntimeError(f"checkpoint book {book!r} has an invalid manifest")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise RuntimeError(f"checkpoint book {book!r} has an unsupported schema")
    if validate_book_key(value["book"], "checkpoint.book") != (
        book.source_name, book.canonical_he_title
    ):
        raise RuntimeError(f"checkpoint book {book!r} identity mismatch")
    if value["source_book_hash"] != source_hash:
        raise RuntimeError(f"checkpoint book {book!r} source hash mismatch")
    for field in ("line_count", "eligible_line_count"):
        if type(value[field]) is not int or value[field] < 0:
            raise RuntimeError(f"checkpoint book {book!r} has invalid {field}")
    if type(value["batches"]) is not list:
        raise RuntimeError(f"checkpoint book {book!r} batches must be an array")
    starts = []
    for index, descriptor in enumerate(value["batches"]):
        _verify_descriptor(root, descriptor, f"checkpoint.book.batches[{index}]")
        starts.append(descriptor["batch_start"])
    if starts != sorted(set(starts)):
        raise RuntimeError(f"checkpoint book {book!r} batch starts are not sorted/unique")


def _prepare_root(args, books, hashes) -> Path:
    root = Path(args.output)
    expected = _checkpoint_document(args, books, hashes)
    checkpoint = root / "checkpoint.json"
    if checkpoint.exists():
        actual = load_json_strict(checkpoint)
        if actual != expected:
            raise RuntimeError("existing NER checkpoint belongs to different immutable inputs")
        _log("driver", "resuming exact validated prior-attempt NER checkpoint")
    else:
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        write_json_atomic(checkpoint, expected)
    for name in ("claims", "partial", "worker-heartbeats"):
        shutil.rmtree(root / name, ignore_errors=True)
    for name in ("claims", "done", "failed", "partial", "worker-heartbeats", "ner-data"):
        (root / name).mkdir(parents=True, exist_ok=True)
    # A book-level engine/API failure is retryable on a new workflow attempt. A clean
    # completed book is trusted only after every content descriptor is revalidated.
    for book in books:
        cid = claim_id(book)
        failed = root / "failed" / cid
        done = root / "done" / cid
        if failed.exists():
            failed.unlink()
            done.unlink(missing_ok=True)
            shutil.rmtree(root / "ner-data" / cid, ignore_errors=True)
        elif done.exists():
            _validate_completed_book(
                root, book, hashes[(book.source_name, book.canonical_he_title)]
            )
    resumed = sum((root / "done" / claim_id(book)).exists() for book in books)
    if resumed:
        _log("driver", f"validated {resumed}/{len(books)} completed book checkpoint(s)")
    return root


def _try_claim(root: Path, cid: str) -> bool:
    claim = root / "claims" / cid
    done = root / "done" / cid
    if done.exists():
        return False
    try:
        claim.mkdir()
    except FileExistsError:
        heartbeat = claim / "heartbeat"
        try:
            if time.time() - heartbeat.stat().st_mtime < CLAIM_STALE_SEC:
                return False
        except OSError:
            pass
        shutil.rmtree(claim, ignore_errors=True)
        try:
            claim.mkdir()
        except FileExistsError:
            return False
    (claim / "heartbeat").touch()
    return True


def _heartbeat(root: Path, cid: str, worker_heartbeat: Path) -> None:
    worker_heartbeat.touch()
    (root / "claims" / cid / "heartbeat").touch()


def _produce_book(args, recognizer, con, book: BookKey, source_hash: str, root: Path, worker_hb: Path) -> None:
    cid = claim_id(book)
    lines = book_lines(con, book)
    book_root = root / "ner-data" / cid
    temporary = root / "partial" / f"{cid}-{os.getpid()}"
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True)
    eligible_count = 0
    batch_descriptors = []
    for start in range(0, len(lines), BATCH_LINES):
        batch = [(line_index, content) for line_index, content in lines[start:start + BATCH_LINES]
                 if content and len(content.strip()) > 1]
        _heartbeat(root, cid, worker_hb)
        if not batch:
            continue
        normalized = recognizer._normalize_input([content for _, content in batch])
        try:
            results = _post_bulk(args.ner_url, normalized)
        except Exception as error:
            _log(args.worker_label, f"bulk failure at {book.canonical_he_title!r}/{start}: {error}; replaying lines")
            _wait_for_ner(args.ner_url, args.worker_label)
            results = []
            for text in normalized:
                results.extend(_post_bulk(args.ner_url, [text]))
        line_records = [
            {
                "line_index": line_index,
                "normalized_sha256": sha256_bytes(text.encode("utf-8")),
                "result": result,
            }
            for (line_index, _), text, result in zip(batch, normalized, results)
        ]
        batch_path = temporary / f"{start:012d}.json"
        size, digest = write_json_atomic(batch_path, {
            "schema_version": SCHEMA_VERSION,
            "book": book.to_dict(),
            "batch_start": start,
            "lines": line_records,
        })
        relative = f"ner-data/{cid}/{batch_path.name}"
        batch_descriptors.append({
            "batch_start": start, "path": relative, "size": size, "sha256": digest,
        })
        eligible_count += len(batch)
    size, digest = write_json_atomic(temporary / "book_manifest.json", {
        "schema_version": SCHEMA_VERSION,
        "book": book.to_dict(),
        "source_book_hash": source_hash,
        "line_count": len(lines),
        "eligible_line_count": eligible_count,
        "batches": batch_descriptors,
    })
    if book_root.exists():
        shutil.rmtree(book_root)
    os.replace(temporary, book_root)
    (root / "done" / cid).touch()
    shutil.rmtree(root / "claims" / cid, ignore_errors=True)
    _log(args.worker_label, f"done {book.source_name}/{book.canonical_he_title!r} lines={len(lines)}")


def worker(args) -> int:
    _, books, hashes = _load_plan(args)
    root = Path(args.output)
    for name in ("claims", "done", "failed", "partial", "worker-heartbeats", "ner-data"):
        (root / name).mkdir(parents=True, exist_ok=True)
    worker_hb = root / "worker-heartbeats" / args.worker_label
    worker_hb.touch()
    # Kaggle is intentionally Mongo-free. Importing LinkerEntityRecognizer would load
    # `sefaria.model` and synchronously probe Mongo even though this phase needs only
    # the training-time normalizer. Keep the exact pinned upstream step sequence here;
    # the resolver later imports the full recognizer to parse RawRefs and resolve them.
    recognizer = build_producer_normalizer()
    con = sqlite3.connect(f"file:{os.path.abspath(args.snapshot)}?mode=ro", uri=True)
    try:
        while True:
            remaining = [book for book in books if not (root / "done" / claim_id(book)).exists()]
            if not remaining:
                return 0
            made_progress = False
            for book in remaining:
                cid = claim_id(book)
                if not _try_claim(root, cid):
                    continue
                made_progress = True
                try:
                    _wait_for_ner(args.ner_url, args.worker_label)
                    _produce_book(args, recognizer, con, book, hashes[(book.source_name, book.canonical_he_title)], root, worker_hb)
                except Exception as error:
                    write_json_atomic(root / "failed" / cid, {
                        "book": book.to_dict(), "error_type": type(error).__name__, "error": str(error),
                    })
                    (root / "done" / cid).touch()
                    shutil.rmtree(root / "claims" / cid, ignore_errors=True)
                    _log(args.worker_label, f"ERROR {book.canonical_he_title!r}: {type(error).__name__}: {error}")
            if not made_progress:
                worker_hb.touch()
                time.sleep(15)
    finally:
        con.close()
        worker_hb.unlink(missing_ok=True)


def finalize(args) -> int:
    _, books, _ = _load_plan(args)
    root = Path(args.output)
    failures = list((root / "failed").glob("*")) if (root / "failed").is_dir() else []
    missing = [book for book in books if not (root / "done" / claim_id(book)).exists()]
    if failures or missing:
        raise RuntimeError(f"NER handoff incomplete: failures={len(failures)} missing={len(missing)}")
    descriptors = []
    for book in books:
        path = root / "ner-data" / claim_id(book) / "book_manifest.json"
        if not path.is_file():
            raise RuntimeError(f"NER handoff missing book manifest for {book!r}")
        descriptors.append({
            "book": book.to_dict(),
            "manifest_path": str(path.relative_to(root)),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    write_json_atomic(root / "ner_manifest.json", {
        "schema_version": SCHEMA_VERSION,
        "relink_request_id": args.relink_request_id,
        "snapshot_sha256": sha256_file(args.snapshot),
        "engine_fingerprint": args.engine_fingerprint,
        "batch_lines": BATCH_LINES,
        "books": descriptors,
    })
    _log("driver", f"finalized NER handoff for {len(books)} book(s)")
    return len(books)


def driver(args) -> int:
    _, books, hashes = _load_plan(args)
    root = _prepare_root(args, books, hashes)
    processes = []
    deadline = (
        time.monotonic() + args.deadline_seconds
        if args.deadline_seconds is not None else None
    )
    try:
        for number in range(1, args.workers + 1):
            command = [sys.executable, os.path.abspath(__file__)] + [
                "--snapshot", os.path.abspath(args.snapshot),
                "--plan", os.path.abspath(args.plan),
                "--output", os.path.abspath(args.output),
                "--relink-request-id", args.relink_request_id,
                "--engine-fingerprint", args.engine_fingerprint,
                "--ner-url", args.ner_url,
                "--worker-label", f"w{number:02d}",
            ]
            processes.append(subprocess.Popen(command, start_new_session=True))
        while any(process.poll() is None for process in processes):
            if deadline is not None and time.monotonic() >= deadline:
                completed = sum(
                    (root / "done" / claim_id(book)).exists() for book in books
                )
                raise TimeoutError(
                    f"NER producer deadline reached with {completed}/{len(books)} "
                    "book checkpoints complete; preserving them for an exact-attempt retry"
                )
            time.sleep(1)
        codes = [process.returncode for process in processes]
    finally:
        live = [process for process in processes if process.poll() is None]
        for process in live:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.time() + 15
        for process in live:
            while process.poll() is None and time.time() < deadline:
                time.sleep(0.25)
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
    if all(code != 0 for code in codes):
        raise RuntimeError(f"all NER workers failed: {codes}")
    return finalize(args)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--relink-request-id", required=True)
    parser.add_argument("--engine-fingerprint", required=True)
    parser.add_argument("--ner-url", default="http://127.0.0.1:5051")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--deadline-seconds", type=int,
        help="stop before the runner ceiling so a partial checkpoint can be uploaded",
    )
    parser.add_argument("--worker-label")
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 8:
        parser.error("--workers must be between 1 and 8")
    if args.deadline_seconds is not None and args.deadline_seconds < 60:
        parser.error("--deadline-seconds must be at least 60")
    if args.worker_label:
        raise SystemExit(worker(args))
    def terminate(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    driver(args)


if __name__ == "__main__":
    main()
