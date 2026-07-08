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

import json
import os
import re
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

SCHEMA_VERSION = 1

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
        )


def validate_record(d: dict) -> None:
    """Raise ValueError if `d` is not a well-formed artifact record. Fails loudly —
    there is no lenient/skip path (see the project's no-heuristics rule)."""
    if not isinstance(d, dict):
        raise ValueError(f"record is not an object: {type(d).__name__}")
    bk = d.get("book_key")
    if not isinstance(bk, dict):
        raise ValueError("book_key missing or not an object")
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
    with open(path, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    return len(recs)
