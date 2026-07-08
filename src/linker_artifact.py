"""Shared contract for LinkerToOtzaria artifacts.

An *artifact record* is one clickable citation link, oriented source → target and
kept deliberately unresolved on the target side: it stores the target **ref**
(e.g. `"Psalms 16:8"`), not a resolved line id. The SeforimLibrary build resolves
each ref to a line at build time (via `resolveRefs`), which is what makes links
survive Sefaria content updates without churn. See `LINKER_DELTA_PLAN.md`.

This module is the single source of truth for the artifact format. Both the linker
engine (`link_books.py`, stage 2) and the incremental driver (stage 3) import it —
never re-implement the schema or the filename scheme elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

SCHEMA_VERSION = 1

_HASH_RE = re.compile(r"[0-9a-f]{16}\Z")


def content_hash(content: str) -> str:
    """Stable digest of a source line's stored content (line.content).

    The linker records it per link so the build can DROP a link whose source line changed
    since the snapshot the offsets were computed against — turning a would-be wrong-offset
    anchor into a safe miss, recovered on the next re-link of that source book. Guards the
    source side the way `resolveRefs` guards the target side. First 16 sha1 hex chars: 64
    bits is ample for a per-line integrity check. Must match the Kotlin importer's digest.
    """
    return hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]

# One JSONL file per source book, under artifacts/<source_name>/<title>.jsonl.
# The authoritative identity is the `book_key` stored inside every record; the
# path is only a stable, filesystem-safe handle. POSIX forbids only "/" and NUL,
# but we also neutralise the Windows-reserved set so the repo checks out anywhere.
_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def _sanitize_component(s: str) -> str:
    out = _UNSAFE.sub("_", s).strip()
    if not out:
        raise ValueError(f"book_key component sanitizes to empty: {s!r}")
    return out


@dataclass(frozen=True)
class BookKey:
    """Matches the generator's `BookKey(sourceName, canonicalHeTitle)` exactly.

    Derived from the DB as `(source.name, COALESCE(book.heRef, book.title))` — the
    same identity the Phase-2 importer maps back to a bookId.
    """

    source_name: str
    canonical_he_title: str

    def to_dict(self) -> dict:
        return {"source_name": self.source_name, "canonical_he_title": self.canonical_he_title}

    @staticmethod
    def from_dict(d: dict) -> "BookKey":
        return BookKey(d["source_name"], d["canonical_he_title"])


@dataclass(frozen=True)
class LinkRecord:
    book_key: BookKey
    line_index: int           # 0-based index into the source book's lines (== line.lineIndex)
    start: int                # offset into the stored line content (line.content), HTML included
    end: int                  # exclusive; end > start
    target_ref: str           # canonical English Sefaria ref, e.g. "Psalms 16:8" — the stable key
    line_index_base: int = 0  # only 0 is supported; explicit for forward-safety
    source_path: Optional[str] = None  # optional, debug only — never authoritative
    source_hash: Optional[str] = None  # content_hash(source line content); build safe-drops on mismatch

    def to_dict(self) -> dict:
        d = {
            "book_key": self.book_key.to_dict(),
            "line_index": self.line_index,
            "line_index_base": self.line_index_base,
            "start": self.start,
            "end": self.end,
            "target_ref": self.target_ref,
        }
        if self.source_path is not None:
            d["source_path"] = self.source_path
        if self.source_hash is not None:
            d["source_hash"] = self.source_hash
        return d

    @staticmethod
    def from_dict(d: dict) -> "LinkRecord":
        validate_record(d)
        return LinkRecord(
            book_key=BookKey.from_dict(d["book_key"]),
            line_index=d["line_index"],
            start=d["start"],
            end=d["end"],
            target_ref=d["target_ref"],
            line_index_base=d.get("line_index_base", 0),
            source_path=d.get("source_path"),
            source_hash=d.get("source_hash"),
        )


_ALLOWED_KEYS = {"book_key", "line_index", "line_index_base", "start", "end", "target_ref", "source_path", "source_hash"}
_ALLOWED_BOOK_KEY_KEYS = {"source_name", "canonical_he_title"}


def validate_record(d: dict) -> None:
    """Raise ValueError if `d` is not a well-formed artifact record. Fails loudly —
    there is no lenient/skip path (see the project's no-heuristics rule). Mirrors
    schema/artifact.schema.json exactly, including additionalProperties: false."""
    if not isinstance(d, dict):
        raise ValueError(f"record is not an object: {type(d).__name__}")
    extra = set(d) - _ALLOWED_KEYS
    if extra:
        raise ValueError(f"unknown field(s): {sorted(extra)}")
    bk = d.get("book_key")
    if not isinstance(bk, dict):
        raise ValueError("book_key missing or not an object")
    bk_extra = set(bk) - _ALLOWED_BOOK_KEY_KEYS
    if bk_extra:
        raise ValueError(f"unknown book_key field(s): {sorted(bk_extra)}")
    for f in ("source_name", "canonical_he_title"):
        v = bk.get(f)
        if not isinstance(v, str) or not v:
            raise ValueError(f"book_key.{f} must be a non-empty string")
    li = d.get("line_index")
    if not isinstance(li, int) or isinstance(li, bool) or li < 0:
        raise ValueError("line_index must be a non-negative integer")
    base = d.get("line_index_base", 0)
    if base != 0:
        raise ValueError(f"line_index_base must be 0 (got {base!r})")
    s, e = d.get("start"), d.get("end")
    for name, v in (("start", s), ("end", e)):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if not (e > s):
        raise ValueError(f"end ({e}) must be greater than start ({s})")
    tr = d.get("target_ref")
    if not isinstance(tr, str) or not tr:
        raise ValueError("target_ref must be a non-empty string")
    sp = d.get("source_path")
    if sp is not None and not isinstance(sp, str):
        raise ValueError("source_path, when present, must be a string")
    sh = d.get("source_hash")
    if sh is not None and (not isinstance(sh, str) or not _HASH_RE.match(sh)):
        raise ValueError("source_hash, when present, must be 16 lowercase hex chars")


def book_key_to_relpath(bk: BookKey) -> str:
    """Deterministic repo-relative path for a book's artifact file."""
    return os.path.join(
        "artifacts",
        _sanitize_component(bk.source_name),
        _sanitize_component(bk.canonical_he_title) + ".jsonl",
    )


def read_artifact(path: str) -> Iterator[LinkRecord]:
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield LinkRecord.from_dict(json.loads(line))
            except Exception as exc:
                raise ValueError(f"{path}:{lineno}: {exc}") from exc


def write_artifact(path: str, records: Iterable[LinkRecord]) -> int:
    """Write all records for a single book to `path`. Asserts every record shares the
    same book_key (one file per book). Returns the number of records written."""
    recs = list(records)
    keys = {r.book_key for r in recs}
    if len(keys) > 1:
        raise ValueError(f"write_artifact: {path} received {len(keys)} distinct book_keys; expected 1")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Atomic: write to a PROCESS-UNIQUE temp then rename, so a crash/steal mid-write never
    # leaves a truncated artifact — readers only ever see a complete file (or the previous
    # one). The pid suffix keeps two workers racing the same book (stale-claim steal) from
    # sharing one temp file and interleaving / breaking each other's os.replace.
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return len(recs)
