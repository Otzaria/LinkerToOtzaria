"""Exact per-line reuse baseline for incremental Linker runs.

The book-level snapshot hash remains the source-change clock.  This companion
baseline is only an optimisation for a book that the clock already marked as
changed: lines whose content fingerprint is identical to a previously linked
line can reuse the old resolved artifact, while only unmatched lines need GPU
NER.  Every current line is assigned to exactly one of those two sets.

The baseline is shipped inside ``linker_links.zst`` rather than committed to
git.  The release asset digest protects the directory in transit; the metadata
and per-book hashes below bind it back to the committed book baseline.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from linker_artifact import BookKey
from linker_artifact import book_key_to_relpath

SCHEMA_VERSION = 1
DIRECTORY_NAME = "line-baseline"
_HEX16 = re.compile(r"[0-9a-f]{16}\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class LineDelta:
    """Partition of a changed book's current lines.

    ``reuse`` contains ``(old_line_index, new_line_index)`` pairs. ``ner_ranges``
    are sorted, non-overlapping, half-open current line-index ranges.  Together
    they must cover every current line exactly once.
    """

    reuse: tuple[tuple[int, int], ...]
    ner_ranges: tuple[tuple[int, int], ...]
    current_line_count: int
    prior_artifact_sha256: str | None = None

    @property
    def reused_line_count(self) -> int:
        return len(self.reuse)

    @property
    def ner_line_count(self) -> int:
        return sum(end - start for start, end in self.ner_ranges)


def line_fingerprint(content: str) -> str:
    """128-bit source-line identity used only for exact reuse matching."""
    return hashlib.blake2b((content or "").encode("utf-8"), digest_size=16).hexdigest()


def _book_id(book: BookKey) -> str:
    return hashlib.sha256(
        f"{book.source_name}\0{book.canonical_he_title}".encode("utf-8")
    ).hexdigest()


def _book_path(root: str | Path, book: BookKey) -> Path:
    return Path(root) / "books" / f"{_book_id(book)}.json"


def _canonical_bytes(value) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _write_json_atomic(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    raw = _canonical_bytes(value)
    with temporary.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _load_json_strict(path: Path):
    def unique(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError(f"duplicate key {key!r}")
            value[key] = item
        return value

    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream, object_pairs_hook=unique)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(f"invalid line baseline {path}: {error}") from error


def coalesce_ranges(indices) -> tuple[tuple[int, int], ...]:
    """Convert sorted/unsorted non-negative indices to half-open ranges."""
    values = sorted(set(indices))
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("line indices must be non-negative integers")
    if not values:
        return ()
    result = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        result.append((start, previous + 1))
        start = previous = value
    result.append((start, previous + 1))
    return tuple(result)


def indices_from_ranges(ranges) -> set[int]:
    result: set[int] = set()
    previous_end = -1
    for item in ranges:
        if (
            type(item) not in (list, tuple)
            or len(item) != 2
            or any(type(value) is not int for value in item)
        ):
            raise RuntimeError("NER ranges must be integer pairs")
        start, end = item
        if start < 0 or end <= start or start < previous_end:
            raise RuntimeError("NER ranges must be sorted, positive and non-overlapping")
        result.update(range(start, end))
        previous_end = end
    return result


def _read_book_rows(
    connection: sqlite3.Connection, book: BookKey
) -> list[tuple[int, str]]:
    rows = connection.execute(
        "SELECT line_index, content FROM lines_snapshot "
        "WHERE source_name=? AND canonical_he_title=? ORDER BY line_index",
        (book.source_name, book.canonical_he_title),
    ).fetchall()
    seen = set()
    result = []
    for line_index, content in rows:
        if type(line_index) is not int or line_index < 0 or line_index in seen:
            raise RuntimeError(f"snapshot has invalid/duplicate line index for {book!r}")
        if content is not None and type(content) is not str:
            raise RuntimeError(f"snapshot has non-text content for {book!r}/{line_index}")
        seen.add(line_index)
        result.append((line_index, content or ""))
    return result


def build_line_baseline(
    snapshot_db: str,
    root: str,
    *,
    current_hashes: dict[tuple[str, str], str],
    snapshot_sha256: str,
    engine_fingerprint: str | None,
    artifacts_root: str,
) -> None:
    """Rebuild the release-only line baseline from an accepted snapshot.

    Rebuilding all books is deliberately simple and fail-closed.  It is linear in
    the snapshot (about the same work as the existing book-hash pass) and happens
    only after every requested line was successfully linked or reused.
    """
    if not _HEX64.fullmatch(snapshot_sha256):
        raise RuntimeError("line baseline requires a full snapshot SHA-256")
    root_path = Path(root)
    temporary = root_path.with_name(root_path.name + f".tmp-{os.getpid()}")
    backup = root_path.with_name(root_path.name + f".old-{os.getpid()}")
    shutil.rmtree(temporary, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    (temporary / "books").mkdir(parents=True)
    connection = sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)
    written = 0
    try:
        for source_name, canonical_he_title in sorted(current_hashes):
            book = BookKey(source_name, canonical_he_title)
            rows = _read_book_rows(connection, book)
            book_hash = current_hashes[(source_name, canonical_he_title)]
            if not _HEX16.fullmatch(book_hash):
                raise RuntimeError(f"invalid current book hash for {book!r}")
            artifact_path = Path(artifacts_root).parent / book_key_to_relpath(book)
            artifact_sha256 = None
            if artifact_path.is_file():
                digest = hashlib.sha256()
                with artifact_path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                artifact_sha256 = digest.hexdigest()
            _write_json_atomic(
                _book_path(temporary, book),
                {
                    "schema_version": SCHEMA_VERSION,
                    "book": book.to_dict(),
                    "book_hash": book_hash,
                    "artifact_sha256": artifact_sha256,
                    "lines": [
                        [line_index, line_fingerprint(content)]
                        for line_index, content in rows
                    ],
                },
            )
            written += 1
    finally:
        connection.close()
    if written != len(current_hashes):
        raise RuntimeError("line baseline book count differs from snapshot baseline")
    _write_json_atomic(
        temporary / "manifest.json",
        {
            "schema_version": SCHEMA_VERSION,
            "snapshot_sha256": snapshot_sha256,
            "engine_fingerprint": engine_fingerprint,
            "book_count": written,
            "line_fingerprint": "blake2b-128",
        },
    )
    if root_path.exists():
        os.replace(root_path, backup)
    try:
        os.replace(temporary, root_path)
    except BaseException:
        if backup.exists() and not root_path.exists():
            os.replace(backup, root_path)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def validate_baseline_identity(
    root: str,
    *,
    snapshot_sha256: str,
    engine_fingerprint: str | None,
    book_count: int,
) -> bool:
    """Return whether the release line baseline matches the committed baseline."""
    try:
        manifest = _load_json_strict(Path(root) / "manifest.json")
    except RuntimeError:
        return False
    return (
        type(manifest) is dict
        and set(manifest)
        == {
            "schema_version",
            "snapshot_sha256",
            "engine_fingerprint",
            "book_count",
            "line_fingerprint",
        }
        and manifest["schema_version"] == SCHEMA_VERSION
        and manifest["snapshot_sha256"] == snapshot_sha256
        and manifest["engine_fingerprint"] == engine_fingerprint
        and manifest["book_count"] == book_count
        and manifest["line_fingerprint"] == "blake2b-128"
    )


def _load_book_baseline(
    root: str, book: BookKey, expected_book_hash: str
) -> tuple[list[tuple[int, str]], str | None]:
    value = _load_json_strict(_book_path(root, book))
    if (
        type(value) is not dict
        or set(value)
        != {"schema_version", "book", "book_hash", "artifact_sha256", "lines"}
        or value["schema_version"] != SCHEMA_VERSION
        or value["book"] != book.to_dict()
        or value["book_hash"] != expected_book_hash
        or (
            value["artifact_sha256"] is not None
            and (
                type(value["artifact_sha256"]) is not str
                or not _HEX64.fullmatch(value["artifact_sha256"])
            )
        )
        or type(value["lines"]) is not list
    ):
        raise RuntimeError(f"line baseline identity mismatch for {book!r}")
    result = []
    seen = set()
    for index, item in enumerate(value["lines"]):
        if (
            type(item) is not list
            or len(item) != 2
            or type(item[0]) is not int
            or item[0] < 0
            or item[0] in seen
            or type(item[1]) is not str
            or not _HEX32.fullmatch(item[1])
        ):
            raise RuntimeError(f"invalid line baseline row {index} for {book!r}")
        seen.add(item[0])
        result.append((item[0], item[1]))
    if [line_index for line_index, _ in result] != sorted(seen):
        raise RuntimeError(f"unsorted line baseline for {book!r}")
    return result, value["artifact_sha256"]


def full_ner_delta(current_rows: list[tuple[int, str]]) -> LineDelta:
    return LineDelta(
        reuse=(),
        ner_ranges=coalesce_ranges(line_index for line_index, _ in current_rows),
        current_line_count=len(current_rows),
        prior_artifact_sha256=None,
    )


def compute_line_delta(
    old_rows: list[tuple[int, str]],
    current_rows: list[tuple[int, str]],
    *,
    prior_artifact_sha256: str | None = None,
) -> LineDelta:
    """Match identical lines one-to-one, preferring stable line indices.

    Exact content is context-free at this layer: the Linker processes each stored
    line as its own document, so an identical moved/duplicated line has identical
    NER and resolution output.  Greedy pairing of remaining identical fingerprints
    is therefore safe and deterministic.
    """
    old_by_index = dict(old_rows)
    current = [
        (line_index, line_fingerprint(content))
        for line_index, content in current_rows
    ]
    used_old = set()
    reuse = []
    unmatched_current = []
    for line_index, fingerprint in current:
        if old_by_index.get(line_index) == fingerprint:
            used_old.add(line_index)
            reuse.append((line_index, line_index))
        else:
            unmatched_current.append((line_index, fingerprint))
    remaining = defaultdict(deque)
    for old_index, fingerprint in old_rows:
        if old_index not in used_old:
            remaining[fingerprint].append(old_index)
    ner = []
    for new_index, fingerprint in unmatched_current:
        candidates = remaining[fingerprint]
        if candidates:
            reuse.append((candidates.popleft(), new_index))
        else:
            ner.append(new_index)
    reuse.sort(key=lambda item: item[1])
    delta = LineDelta(
        reuse=tuple(reuse),
        ner_ranges=coalesce_ranges(ner),
        current_line_count=len(current_rows),
        prior_artifact_sha256=prior_artifact_sha256,
    )
    current_indices = {line_index for line_index, _ in current_rows}
    reused_destinations = {new for _, new in delta.reuse}
    ner_indices = indices_from_ranges(delta.ner_ranges)
    if (
        len(reused_destinations) != len(delta.reuse)
        or reused_destinations & ner_indices
        or reused_destinations | ner_indices != current_indices
    ):
        raise RuntimeError("line delta does not partition the current book exactly")
    return delta


def plan_changed_books(
    snapshot_db: str,
    changed: list[BookKey],
    *,
    baseline_root: str,
    baseline_hashes: dict[tuple[str, str], str],
    baseline_identity_valid: bool,
) -> tuple[dict[tuple[str, str], LineDelta], int, int]:
    """Plan safe per-line reuse for changed books; fall back per book on any drift."""
    connection = sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)
    plans = {}
    reused = 0
    ner = 0
    try:
        for book in changed:
            current_rows = _read_book_rows(connection, book)
            key = (book.source_name, book.canonical_he_title)
            delta = None
            if baseline_identity_valid and key in baseline_hashes:
                try:
                    old_rows, artifact_sha256 = _load_book_baseline(
                        baseline_root, book, baseline_hashes[key]
                    )
                    delta = compute_line_delta(
                        old_rows,
                        current_rows,
                        prior_artifact_sha256=artifact_sha256,
                    )
                except RuntimeError:
                    # The optimisation is optional.  A missing/corrupt/mismatched
                    # per-book baseline must cost GPU time, never correctness.
                    delta = None
            if delta is None:
                delta = full_ner_delta(current_rows)
            plans[key] = delta
            reused += delta.reused_line_count
            ner += delta.ner_line_count
    finally:
        connection.close()
    return plans, reused, ner
