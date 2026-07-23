"""Strict, content-addressed contract between GPU NER and CPU resolution.

The handoff intentionally contains only raw model output.  Python/Sefaria objects are
never pickled across machines: the resolver reconstructs them with the exact pinned
Sefaria parser after validating the snapshot, normalizer output, plan and every shard.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
HEX64 = re.compile(r"[0-9a-f]{64}")


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_object(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def load_json_strict(path: str | Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream, object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(f"invalid JSON contract {path}: {error}") from error


def write_json_atomic(path: str | Path, value: Any) -> tuple[int, str]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical_json(value)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    with temporary.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return len(raw), sha256_bytes(raw)


def validate_book_key(value: Any, where: str) -> tuple[str, str]:
    if type(value) is not dict or set(value) != {"source_name", "canonical_he_title"}:
        raise RuntimeError(f"{where}: invalid book key shape")
    source = value["source_name"]
    title = value["canonical_he_title"]
    if type(source) is not str or not source or "\x00" in source:
        raise RuntimeError(f"{where}: invalid source_name")
    if type(title) is not str or not title or "\x00" in title:
        raise RuntimeError(f"{where}: invalid canonical_he_title")
    return source, title


def validate_plan(value: Any, *, request_id: str, snapshot_sha256: str, engine_fingerprint: str) -> dict:
    required = {
        "schema_version", "relink_request_id", "snapshot_sha256", "changelog_sha256",
        "engine_fingerprint", "changed", "removed", "current_books",
    }
    if type(value) is not dict or set(value) != required:
        raise RuntimeError("NER plan has an invalid key set")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise RuntimeError("NER plan has an unsupported schema_version")
    for actual, expected, label in (
        (value["relink_request_id"], request_id, "relink_request_id"),
        (value["snapshot_sha256"], snapshot_sha256, "snapshot_sha256"),
        (value["engine_fingerprint"], engine_fingerprint, "engine_fingerprint"),
    ):
        if type(actual) is not str or actual != expected:
            raise RuntimeError(f"NER plan {label} does not match the current run")
    if not HEX64.fullmatch(request_id) or not HEX64.fullmatch(snapshot_sha256):
        raise RuntimeError("current run supplied an invalid request/snapshot digest")
    changelog = value["changelog_sha256"]
    if changelog is not None and (type(changelog) is not str or not HEX64.fullmatch(changelog)):
        raise RuntimeError("NER plan changelog_sha256 is invalid")
    for field in ("changed", "removed"):
        if type(value[field]) is not list:
            raise RuntimeError(f"NER plan {field} must be an array")
        seen = set()
        for index, item in enumerate(value[field]):
            key = validate_book_key(item, f"plan.{field}[{index}]")
            if key in seen:
                raise RuntimeError(f"NER plan {field} contains a duplicate book")
            seen.add(key)
    if type(value["current_books"]) is not list:
        raise RuntimeError("NER plan current_books must be an array")
    seen = set()
    for index, item in enumerate(value["current_books"]):
        if type(item) is not dict or set(item) != {"source_name", "canonical_he_title", "hash"}:
            raise RuntimeError(f"plan.current_books[{index}] has an invalid shape")
        key = validate_book_key(
            {"source_name": item["source_name"], "canonical_he_title": item["canonical_he_title"]},
            f"plan.current_books[{index}]",
        )
        if key in seen or type(item["hash"]) is not str or not re.fullmatch(r"[0-9a-f]{16}", item["hash"]):
            raise RuntimeError(f"plan.current_books[{index}] is duplicate or has an invalid hash")
        seen.add(key)
    return value


def _validate_range(value: Any, limit: int, where: str) -> tuple[int, int]:
    if (type(value) is not list or len(value) != 2
            or any(type(item) is not int for item in value)):
        raise RuntimeError(f"{where}: range must contain exactly two integers")
    start, end = value
    if start < 0 or end < start or end > limit:
        raise RuntimeError(f"{where}: range [{start},{end}] exceeds text length {limit}")
    return start, end


def validate_ner_result(result: Any, normalized_text: str, where: str) -> dict:
    if type(result) is not dict or set(result) != {"entities"} or type(result["entities"]) is not list:
        raise RuntimeError(f"{where}: invalid GPU result shape")
    for entity_index, entity in enumerate(result["entities"]):
        location = f"{where}.entities[{entity_index}]"
        if type(entity) is not dict or set(entity) not in ({"label", "range"}, {"label", "range", "parts"}):
            raise RuntimeError(f"{location}: invalid entity shape")
        if type(entity["label"]) is not str or not entity["label"] or len(entity["label"]) > 200:
            raise RuntimeError(f"{location}: invalid label")
        start, end = _validate_range(entity["range"], len(normalized_text), location)
        parts = entity.get("parts")
        if parts is not None:
            if type(parts) is not list:
                raise RuntimeError(f"{location}: parts must be an array")
            citation_len = end - start
            for part_index, part in enumerate(parts):
                part_location = f"{location}.parts[{part_index}]"
                if type(part) is not dict or set(part) != {"label", "range"}:
                    raise RuntimeError(f"{part_location}: invalid part shape")
                if type(part["label"]) is not str or not part["label"] or len(part["label"]) > 200:
                    raise RuntimeError(f"{part_location}: invalid label")
                _validate_range(part["range"], citation_len, part_location)
    return result


def safe_relative_path(value: Any, where: str) -> str:
    if type(value) is not str:
        raise RuntimeError(f"{where}: path is not a string")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or any("\x00" in part for part in path.parts):
        raise RuntimeError(f"{where}: unsafe relative path")
    return str(path)


def validate_batch(
    value: Any,
    *,
    expected_book: tuple[str, str],
    expected_start: int,
    normalized_lines: list[tuple[int, str]],
) -> dict:
    required = {"schema_version", "book", "batch_start", "lines"}
    if type(value) is not dict or set(value) != required:
        raise RuntimeError("NER batch has an invalid key set")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise RuntimeError("NER batch has an unsupported schema_version")
    if validate_book_key(value["book"], "batch.book") != expected_book:
        raise RuntimeError("NER batch belongs to a different book")
    if type(value["batch_start"]) is not int or value["batch_start"] != expected_start:
        raise RuntimeError("NER batch_start does not match its ledger")
    if type(value["lines"]) is not list or len(value["lines"]) != len(normalized_lines):
        raise RuntimeError("NER batch line count does not match the current snapshot batch")
    for index, (line, (expected_line_index, normalized_text)) in enumerate(zip(value["lines"], normalized_lines)):
        if type(line) is not dict or set(line) != {"line_index", "normalized_sha256", "result"}:
            raise RuntimeError(f"NER batch line {index} has an invalid shape")
        if type(line["line_index"]) is not int or line["line_index"] != expected_line_index:
            raise RuntimeError(f"NER batch line {index} has the wrong line_index")
        expected_digest = sha256_bytes(normalized_text.encode("utf-8"))
        if type(line["normalized_sha256"]) is not str or line["normalized_sha256"] != expected_digest:
            raise RuntimeError(f"NER batch line {index} normalization digest mismatch")
        validate_ner_result(line["result"], normalized_text, f"batch.lines[{index}].result")
    return value


class NerBundle:
    """Fail-closed reader for a fully extracted NER handoff."""

    def __init__(
        self,
        root: str | Path,
        *,
        request_id: str,
        snapshot_sha256: str,
        engine_fingerprint: str,
        changed_books: list[dict],
        expected_book_hashes: dict[tuple[str, str], str] | None = None,
        expected_batch_lines: int | None = None,
    ):
        self.root = Path(root).resolve()
        manifest = load_json_strict(self.root / "ner_manifest.json")
        required = {
            "schema_version", "relink_request_id", "snapshot_sha256", "engine_fingerprint",
            "batch_lines", "books",
        }
        if type(manifest) is not dict or set(manifest) != required:
            raise RuntimeError("NER manifest has an invalid key set")
        if type(manifest["schema_version"]) is not int or manifest["schema_version"] != SCHEMA_VERSION:
            raise RuntimeError("NER manifest has an unsupported schema_version")
        if manifest["relink_request_id"] != request_id:
            raise RuntimeError("NER manifest request identity mismatch")
        if manifest["snapshot_sha256"] != snapshot_sha256:
            raise RuntimeError("NER manifest snapshot digest mismatch")
        if manifest["engine_fingerprint"] != engine_fingerprint:
            raise RuntimeError("NER manifest engine fingerprint mismatch")
        if type(manifest["batch_lines"]) is not int or manifest["batch_lines"] <= 0:
            raise RuntimeError("NER manifest batch_lines is invalid")
        if expected_batch_lines is not None and manifest["batch_lines"] != expected_batch_lines:
            raise RuntimeError("NER manifest batch size differs from resolver transport boundary")
        expected = [validate_book_key(item, f"changed_books[{index}]")
                    for index, item in enumerate(changed_books)]
        books = manifest["books"]
        if type(books) is not list:
            raise RuntimeError("NER manifest books must be an array")
        self.books: dict[tuple[str, str], dict] = {}
        for index, descriptor in enumerate(books):
            if type(descriptor) is not dict or set(descriptor) != {"book", "manifest_path", "size", "sha256"}:
                raise RuntimeError(f"NER manifest book descriptor {index} has an invalid shape")
            key = validate_book_key(descriptor["book"], f"manifest.books[{index}].book")
            if key in self.books:
                raise RuntimeError("NER manifest contains a duplicate book")
            relative = safe_relative_path(descriptor["manifest_path"], f"manifest.books[{index}].manifest_path")
            path = self._below_root(relative)
            self._verify_descriptor(path, descriptor, f"manifest.books[{index}]")
            book_manifest = load_json_strict(path)
            self._validate_book_manifest(book_manifest, key)
            if expected_book_hashes is not None and book_manifest["source_book_hash"] != expected_book_hashes.get(key):
                raise RuntimeError(f"NER book manifest source hash differs from snapshot: {key!r}")
            self.books[key] = book_manifest
        if list(self.books) != expected:
            raise RuntimeError("NER manifest book order/set differs from the resolver changed-book plan")
        self.batch_lines = manifest["batch_lines"]

    def _below_root(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise RuntimeError(f"NER path escapes bundle root: {relative!r}") from error
        return path

    @staticmethod
    def _verify_descriptor(path: Path, descriptor: dict, where: str) -> None:
        if type(descriptor["size"]) is not int or descriptor["size"] <= 0:
            raise RuntimeError(f"{where}: invalid size")
        if type(descriptor["sha256"]) is not str or not HEX64.fullmatch(descriptor["sha256"]):
            raise RuntimeError(f"{where}: invalid sha256")
        try:
            size = path.stat().st_size
        except OSError as error:
            raise RuntimeError(f"{where}: missing file {path}") from error
        if size != descriptor["size"] or sha256_file(path) != descriptor["sha256"]:
            raise RuntimeError(f"{where}: content descriptor mismatch")

    def _validate_book_manifest(self, value: Any, expected_book: tuple[str, str]) -> None:
        required = {"schema_version", "book", "source_book_hash", "line_count", "eligible_line_count", "batches"}
        if type(value) is not dict or set(value) != required:
            raise RuntimeError("NER book manifest has an invalid key set")
        if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
            raise RuntimeError("NER book manifest has an unsupported schema_version")
        if validate_book_key(value["book"], "book_manifest.book") != expected_book:
            raise RuntimeError("NER book manifest identity mismatch")
        if type(value["source_book_hash"]) is not str or not re.fullmatch(r"[0-9a-f]{16}", value["source_book_hash"]):
            raise RuntimeError("NER book manifest source_book_hash is invalid")
        for field in ("line_count", "eligible_line_count"):
            if type(value[field]) is not int or value[field] < 0:
                raise RuntimeError(f"NER book manifest {field} is invalid")
        if type(value["batches"]) is not list:
            raise RuntimeError("NER book manifest batches must be an array")
        starts = []
        for index, batch in enumerate(value["batches"]):
            if type(batch) is not dict or set(batch) != {"batch_start", "path", "size", "sha256"}:
                raise RuntimeError(f"NER batch descriptor {index} has an invalid shape")
            if type(batch["batch_start"]) is not int or batch["batch_start"] < 0:
                raise RuntimeError(f"NER batch descriptor {index} has an invalid start")
            starts.append(batch["batch_start"])
            relative = safe_relative_path(batch["path"], f"book_manifest.batches[{index}].path")
            self._verify_descriptor(self._below_root(relative), batch, f"book_manifest.batches[{index}]")
        if starts != sorted(set(starts)):
            raise RuntimeError("NER book manifest batch starts are not unique and sorted")

    def resolve_batch(self, linker, book, batch: list[tuple[int, str]], batch_start: int):
        key = (book.source_name, book.canonical_he_title)
        book_manifest = self.books.get(key)
        if book_manifest is None:
            raise RuntimeError(f"no NER handoff for {key!r}")
        descriptor = next(
            (item for item in book_manifest["batches"] if item["batch_start"] == batch_start),
            None,
        )
        if descriptor is None:
            raise RuntimeError(f"NER handoff is missing batch {batch_start} for {key!r}")
        path = self._below_root(descriptor["path"])
        self._verify_descriptor(path, descriptor, f"batch {key!r}/{batch_start}")
        original_ner = linker.get_ner()
        normalized = original_ner._normalize_input([content for _, content in batch])
        payload = validate_batch(
            load_json_strict(path),
            expected_book=key,
            expected_start=batch_start,
            normalized_lines=[(line_index, text) for (line_index, _), text in zip(batch, normalized)],
        )

        class ReplayRecognizer:
            normalizer = original_ner.normalizer

            def bulk_recognize(self, inputs):
                current = original_ner._normalize_input(inputs)
                if current != normalized:
                    raise RuntimeError("resolver inputs differ from the validated NER batch")
                entities = []
                for text, line in zip(current, payload["lines"]):
                    raw_refs, non_citations = original_ner._parse_recognize_response(text, line["result"])
                    entities.append(raw_refs + non_citations)
                return entities

        # Use the pinned upstream bulk_link unchanged.  Only the recognizer transport is
        # replaced; resolution, ibid reset and normal→original offset mapping are identical.
        original = linker._ner
        linker._ner = ReplayRecognizer()
        try:
            return linker.bulk_link([content for _, content in batch], type_filter="citation")
        finally:
            linker._ner = original
