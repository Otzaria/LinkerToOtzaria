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

# The manifest stays at 2: SeforimLibrary's recovery check pins it.
SCHEMA_VERSION = 2
# Book files: 3 adds book_context, ibid_seams and a per-line NER-entity flag.
BOOK_SCHEMA_VERSION = 3
DIRECTORY_NAME = "line-baseline"
_HEX16 = re.compile(r"[0-9a-f]{16}\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
# ``שם`` is resolved from the preceding citation(s), so an unchanged line can no
# longer be reused safely when another line in the same book changed.  Keep this
# deliberately broad: a needless full-book pass costs time; a stale ibid target is
# a silent wrong link.  The linker is still free to reject ordinary uses of the word.
_IBID_TOKEN_RE = re.compile(r"(?<![א-ת])שם(?![א-ת])")
# Canonical Hebrew Bavli tractate titles; the planner must not import Sefaria.
# The resolver shares this gate and still validates the actual index metadata.
_BAVLI_HE_TITLES = frozenset({
    "ברכות", "שבת", "עירובין", "פסחים", "ראש השנה", "יומא", "סוכה", "ביצה",
    "תענית", "מגילה", "מועד קטן", "חגיגה", "יבמות", "כתובות", "נדרים", "נזיר",
    "סוטה", "גיטין", "קידושין", "בבא קמא", "בבא מציעא", "בבא בתרא", "סנהדרין",
    "מכות", "שבועות", "עבודה זרה", "הוריות", "זבחים", "מנחות", "חולין", "בכורות",
    "ערכין", "תמורה", "כריתות", "מעילה", "תמיד", "נדה",
})


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
    # Set on an ibid book planned as a context probe: the whole book, which the NER
    # producer covers instead once any probed line carries an entity.
    context_ranges: tuple[tuple[int, int], ...] | None = None

    @property
    def reused_line_count(self) -> int:
        return len(self.reuse)

    @property
    def ner_line_count(self) -> int:
        return sum(end - start for start, end in self.ner_ranges)


def line_fingerprint(content: str, context_ref: str) -> str:
    """128-bit exact identity for output-affecting source-line inputs."""
    digest = hashlib.blake2b(digest_size=16)
    digest.update((content or "").encode("utf-8"))
    digest.update(b"\0")
    digest.update((context_ref or "").encode("utf-8"))
    return digest.hexdigest()


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
) -> list[tuple[int, str, str]]:
    rows = connection.execute(
        "SELECT line_index, content, context_ref FROM lines_snapshot "
        "WHERE source_name=? AND canonical_he_title=? ORDER BY line_index",
        (book.source_name, book.canonical_he_title),
    ).fetchall()
    seen = set()
    result = []
    for line_index, content, context_ref in rows:
        if type(line_index) is not int or line_index < 0 or line_index in seen:
            raise RuntimeError(f"snapshot has invalid/duplicate line index for {book!r}")
        if content is not None and type(content) is not str:
            raise RuntimeError(f"snapshot has non-text content for {book!r}/{line_index}")
        if not isinstance(context_ref, str) or not context_ref.strip():
            raise RuntimeError(f"snapshot has invalid context for {book!r}/{line_index}")
        seen.add(line_index)
        result.append((line_index, content or "", context_ref))
    return result


def build_line_baseline(
    snapshot_db: str,
    root: str,
    *,
    current_hashes: dict[tuple[str, str], str],
    snapshot_sha256: str,
    engine_fingerprint: str | None,
    artifacts_root: str,
    book_state=None,
) -> int:
    """Rebuild the release-only line baseline from an accepted snapshot.

    ``book_state(book, rows)`` returns the per-row NER-entity flags (``None`` when
    unknown) and the ibid seams the book's artifact was linked under; without it
    both are unknown.

    Rebuilding all books is deliberately simple and fail-closed.  It is linear in
    the snapshot (about the same work as the existing book-hash pass) and happens
    only after every requested line was successfully linked or reused.

    Returns the number of link records in the WHOLE published artifact store — the
    number the release provenance reports (``relink_work.json``).  It is free here
    and nowhere else: this pass already streams every artifact byte to digest it, so
    the count costs one ``bytes.count`` per chunk instead of a second full pass over
    a 667 MiB store.  It is exact rather than an estimate because
    ``linker_artifact.write_artifact`` writes one ``json.dumps`` object per line and
    terminates EVERY record with ``\\n`` (a record can contain no raw newline — JSON
    escapes it), and a book with zero links has no file at all.
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
    link_records = 0
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
                        link_records += chunk.count(b"\n")
                artifact_sha256 = digest.hexdigest()
            flags, seams = ([None] * len(rows), None) if book_state is None else book_state(book, rows)
            flags = list(flags)
            if len(flags) != len(rows) or any(
                flag is not None and type(flag) is not bool for flag in flags
            ):
                raise RuntimeError(f"invalid line entity flags for {book!r}")
            if seams is not None and not _valid_seams(seams):
                raise RuntimeError(f"invalid ibid seams for {book!r}")
            _write_json_atomic(
                _book_path(temporary, book),
                {
                    "schema_version": BOOK_SCHEMA_VERSION,
                    "book": book.to_dict(),
                    "book_hash": book_hash,
                    "artifact_sha256": artifact_sha256,
                    "book_context": requires_book_context(rows),
                    "ibid_seams": seams,
                    "lines": [
                        [line_index, line_fingerprint(content, context_ref), flag]
                        for (line_index, content, context_ref), flag in zip(rows, flags)
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
    # Only after the swap succeeded: a caller must never record a link count for a
    # baseline that was not actually published.
    return link_records


def ibid_seam_counts(rows, flags, seams) -> list[int] | None:
    """Entity lines before each transport seam of a whole-book stateful pass.

    IbidContext carries only the last refs across a seam, not Sefaria's title map, so
    a stateful book's links depend on these counts as well as on its entity lines.
    """
    if any(flag is None for flag in flags):
        return None
    seam_lines = set(seams)
    counts = set()
    entities = 0
    for (line_index, _, _), flag in zip(rows, flags):
        # A seam before the first entity restores an empty history: a no-op.
        if line_index in seam_lines and entities:
            counts.add(entities)
        entities += flag
    return sorted(counts)


def _valid_seams(value) -> bool:
    return (
        type(value) is list
        and all(type(item) is int and item >= 0 for item in value)
        and value == sorted(set(value))
    )


def next_book_state(
    *,
    baseline_root: str,
    baseline_hashes: dict[tuple[str, str], str],
    line_deltas: dict[tuple[str, str], LineDelta],
    bundle,
    ner_eligible,
    batch_seams,
):
    """``book_state`` for build_line_baseline after a successful run.

    Flags and seams describe the artifact: NER'd lines take the bundle's flag, reused
    and unchanged lines keep theirs, an unchanged book keeps its seams and a relinked
    stateful book gets those of its current layout.  Pass no hashes when the prior
    line baseline is not valid for this engine.
    """

    def state(book: BookKey, rows):
        # Only a book with book context is ever probed.
        if not requires_book_context(rows):
            return [None] * len(rows), None
        key = (book.source_name, book.canonical_he_title)
        delta = line_deltas.get(key)
        if delta is None:
            carried = {line_index: line_index for line_index, _, _ in rows}
            sent: set[int] = set()
            found = None
        else:
            carried = {new: old for old, new in delta.reuse}
            escalated = bundle is not None and key in bundle.escalated
            sent = indices_from_ranges(delta.context_ranges if escalated else delta.ner_ranges)
            found = bundle.entity_lines(key) if bundle is not None else None
        old = None
        if carried and key in baseline_hashes:
            try:
                old = _load_book_baseline(baseline_root, book, baseline_hashes[key])
            except RuntimeError:
                old = None
        flags = []
        for line_index, content, _context_ref in rows:
            if line_index in sent:
                if found is None:
                    flags.append(None)
                elif line_index in found:
                    flags.append(found[line_index])
                elif not ner_eligible(content):
                    flags.append(False)
                else:
                    raise RuntimeError(f"NER bundle lacks planned line {line_index} of {book!r}")
            elif line_index in carried and old is not None:
                flags.append(old.entities.get(carried[line_index]))
            else:
                flags.append(None)
        if delta is None:
            seams = old.ibid_seams if old is not None else None
        else:
            seams = ibid_seam_counts(rows, flags, batch_seams(rows))
        return flags, seams

    return state


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


@dataclass(frozen=True)
class BookBaseline:
    """One prior book: line fingerprints, artifact digest and, from schema 3 on,
    whether it was resolved with book context and which lines carried NER entities."""

    lines: list[tuple[int, str]]
    artifact_sha256: str | None
    entities: dict[int, bool | None]
    book_context: bool | None
    ibid_seams: list[int] | None


_BOOK_KEYS = {
    2: {"schema_version", "book", "book_hash", "artifact_sha256", "lines"},
    3: {
        "schema_version", "book", "book_hash", "artifact_sha256", "book_context",
        "ibid_seams", "lines",
    },
}


def _load_book_baseline(
    root: str, book: BookKey, expected_book_hash: str
) -> BookBaseline:
    value = _load_json_strict(_book_path(root, book))
    schema = value.get("schema_version") if type(value) is dict else None
    if (
        type(schema) is not int
        or schema not in _BOOK_KEYS
        or set(value) != _BOOK_KEYS[schema]
        or (schema == 3 and type(value["book_context"]) is not bool)
        or (schema == 3 and value["ibid_seams"] is not None
            and not _valid_seams(value["ibid_seams"]))
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
    width = 3 if schema == 3 else 2
    result = []
    entities = {}
    seen = set()
    for index, item in enumerate(value["lines"]):
        if (
            type(item) is not list
            or len(item) != width
            or type(item[0]) is not int
            or item[0] < 0
            or item[0] in seen
            or type(item[1]) is not str
            or not _HEX32.fullmatch(item[1])
            or (width == 3 and item[2] is not None and type(item[2]) is not bool)
        ):
            raise RuntimeError(f"invalid line baseline row {index} for {book!r}")
        seen.add(item[0])
        result.append((item[0], item[1]))
        entities[item[0]] = item[2] if width == 3 else None
    if [line_index for line_index, _ in result] != sorted(seen):
        raise RuntimeError(f"unsorted line baseline for {book!r}")
    return BookBaseline(
        lines=result,
        artifact_sha256=value["artifact_sha256"],
        entities=entities,
        book_context=value["book_context"] if schema == 3 else None,
        ibid_seams=value["ibid_seams"] if schema == 3 else None,
    )


def full_ner_delta(current_rows: list[tuple[int, str, str]]) -> LineDelta:
    return LineDelta(
        reuse=(),
        ner_ranges=coalesce_ranges(line_index for line_index, _, _ in current_rows),
        current_line_count=len(current_rows),
        prior_artifact_sha256=None,
    )


def placed_by_daf_headings(book: BookKey) -> bool:
    """Whether link_books.talmud_positions may place this book's lines by its daf headings.

    Editing or deleting such a heading changes the lines below it, which a per-line
    plan cannot see in the old rows, so these books are always resolved whole.
    """
    if book.source_name in ("Sefaria", "KSK"):
        return False
    _commentary, separator, tractate = (book.canonical_he_title or "").rpartition(" על ")
    return bool(separator) and tractate in _BAVLI_HE_TITLES


def requires_book_context(current_rows: list[tuple[int, str, str]]) -> bool:
    """Whether resolving this book needs preceding-line citation history.

    Sefaria's ibid resolver carries the last references across inputs.  A line plan
    that sends only changed rows would omit that history, and reusing an unchanged
    ibid line would preserve a target selected from obsolete history.  The Hebrew
    token is intentionally a conservative, cheap pre-NER gate.
    """
    return any(_IBID_TOKEN_RE.search(content or "") for _, content, _ in current_rows)


def compute_line_delta(
    old_rows: list[tuple[int, str]],
    current_rows: list[tuple[int, str, str]],
    *,
    prior_artifact_sha256: str | None = None,
    old_entities: dict[int, bool | None] | None = None,
    old_book_context: bool | None = None,
    old_ibid_seams: list[int] | None = None,
    batch_seams=None,
) -> LineDelta:
    """Match identical lines one-to-one, preferring stable line indices.

    Both content and context are output-affecting. An identical line moved to a
    different Sefaria location must be re-resolved because לעיל/לקמן can change.
    """
    if requires_book_context(current_rows):
        return _context_probe_delta(
            old_rows, current_rows, prior_artifact_sha256, old_entities, old_book_context,
            old_ibid_seams, batch_seams,
        ) or full_ner_delta(current_rows)
    reuse, ner = _match_lines(old_rows, current_rows)
    return _checked_delta(current_rows, reuse, ner, prior_artifact_sha256)


def _context_probe_delta(
    old_rows, current_rows, prior_artifact_sha256, old_entities, old_book_context,
    old_ibid_seams, batch_seams,
) -> LineDelta | None:
    """Plan an ibid book as a context probe, or return None for a whole-book pass.

    Only lines with a NER entity move ibid history, so if dropped old lines had none,
    entity lines keep their order and the transport seams fall between the same entity
    lines, unchanged lines keep their history — provided the new lines have no entity
    either, which the NER producer checks.
    """
    if (old_book_context is not True or old_entities is None or old_ibid_seams is None
            or batch_seams is None or not current_rows):
        return None
    if any(old_entities.get(line_index) is None for line_index, _ in old_rows):
        return None
    reuse, ner = _match_lines(old_rows, current_rows)
    if not reuse:
        return None
    used_old = {old for old, _ in reuse}
    if any(old_entities[line_index] for line_index, _ in old_rows if line_index not in used_old):
        return None
    carried = [old for old, _ in reuse if old_entities[old]]
    if any(later <= earlier for earlier, later in zip(carried, carried[1:])):
        return None
    reused_from = {new: old for old, new in reuse}
    current_flags = [
        old_entities[reused_from[line_index]] if line_index in reused_from else False
        for line_index, _, _ in current_rows
    ]
    if ibid_seam_counts(current_rows, current_flags, batch_seams(current_rows)) != old_ibid_seams:
        return None
    return _checked_delta(
        current_rows, reuse, ner, prior_artifact_sha256,
        context_ranges=coalesce_ranges(line_index for line_index, _, _ in current_rows),
    )


def _match_lines(old_rows, current_rows) -> tuple[list[tuple[int, int]], list[int]]:
    old_by_index = dict(old_rows)
    current = [
        (line_index, line_fingerprint(content, context_ref))
        for line_index, content, context_ref in current_rows
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
    return reuse, ner


def _checked_delta(
    current_rows, reuse, ner, prior_artifact_sha256, context_ranges=None,
) -> LineDelta:
    delta = LineDelta(
        reuse=tuple(reuse),
        ner_ranges=coalesce_ranges(ner),
        current_line_count=len(current_rows),
        prior_artifact_sha256=prior_artifact_sha256,
        context_ranges=context_ranges,
    )
    current_indices = {line_index for line_index, _, _ in current_rows}
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
    batch_seams=None,
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
            if (baseline_identity_valid and key in baseline_hashes
                    and not placed_by_daf_headings(book)):
                try:
                    old = _load_book_baseline(
                        baseline_root, book, baseline_hashes[key]
                    )
                    delta = compute_line_delta(
                        old.lines,
                        current_rows,
                        prior_artifact_sha256=old.artifact_sha256,
                        old_entities=old.entities,
                        old_book_context=old.book_context,
                        old_ibid_seams=old.ibid_seams,
                        batch_seams=batch_seams,
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
