#!/usr/bin/env python3
"""Seed the release-only line baseline without running the linker engine.

This is a one-time migration path for a snapshot that is already fully linked.
It refuses to operate unless the committed book baseline, committed lineage,
snapshot bytes, and every existing artifact record agree exactly. It then builds
the optimisation index and emits the already-committed engine components expected
by the normal publisher contract.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from incremental import (  # noqa: E402
    _read_baseline_file,
    sha256_of_file,
    snapshot_book_hashes,
)
from line_baseline import DIRECTORY_NAME, build_line_baseline  # noqa: E402
from linker_artifact import (  # noqa: E402
    book_key_to_relpath,
    content_hash,
    read_artifact,
)


def _load_json(path: Path):
    def unique(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError(f"duplicate JSON key {key!r}")
            value[key] = item
        return value

    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream, object_pairs_hook=unique)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(f"invalid JSON {path}: {error}") from error


def _engine_components(fingerprint: str) -> list[str]:
    if not isinstance(fingerprint, str):
        raise RuntimeError("committed engine fingerprint is not a string")
    parts = fingerprint.split(";")
    if len(parts) < 3 or parts[-2:] != ["policy=drop", "bavli=0"]:
        raise RuntimeError("committed engine fingerprint has an unsupported policy suffix")
    components = parts[:-2]
    if (
        any(not item or "\n" in item or "\r" in item or "=" not in item for item in components)
        or len({item.partition("=")[0] for item in components}) != len(components)
    ):
        raise RuntimeError("committed engine fingerprint components are malformed")
    reconstructed = "".join(f"{item};" for item in sorted(components)) + "policy=drop;bavli=0"
    if reconstructed != fingerprint:
        raise RuntimeError("committed engine fingerprint is not canonical")
    return components


def _validate_artifacts(
    repo: Path,
    snapshot: Path,
    baseline: dict[tuple[str, str], str],
) -> tuple[int, int]:
    artifacts = repo / "artifacts"
    if not artifacts.is_dir():
        raise RuntimeError("restored artifact directory is missing")
    connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    files = records = 0
    try:
        for path in sorted(artifacts.rglob("*")):
            if path.is_dir():
                continue
            relative = path.relative_to(repo)
            if relative.as_posix() == "artifacts/.gitkeep":
                continue
            if path.suffix != ".jsonl":
                raise RuntimeError(f"unexpected artifact payload file: {relative}")
            book = None
            contents = None
            count = 0
            for record in read_artifact(str(path)):
                count += 1
                records += 1
                if book is None:
                    book = record.book_key
                    key = (book.source_name, book.canonical_he_title)
                    if key not in baseline:
                        raise RuntimeError(
                            f"artifact belongs to a book outside the baseline: {key!r}"
                        )
                    if Path(book_key_to_relpath(book)) != relative:
                        raise RuntimeError(
                            f"artifact path does not match its book identity: {relative}"
                        )
                    rows = connection.execute(
                        "SELECT line_index, content, context_ref FROM lines_snapshot "
                        "WHERE source_name=? AND canonical_he_title=?",
                        key,
                    ).fetchall()
                    contents = {
                        line_index: (content or "", context_ref)
                        for line_index, content, context_ref in rows
                    }
                    if len(contents) != len(rows):
                        raise RuntimeError(f"snapshot has duplicate line indices for {key!r}")
                elif record.book_key != book:
                    raise RuntimeError(f"artifact mixes multiple book identities: {relative}")
                source = contents.get(record.line_index)
                if source is None:
                    raise RuntimeError(
                        f"artifact references an absent source line: "
                        f"{relative}/{record.line_index}"
                    )
                content, context_ref = source
                if record.source_hash != content_hash(content):
                    raise RuntimeError(
                        f"artifact source hash differs from snapshot: "
                        f"{relative}/{record.line_index}"
                    )
                if record.context_ref is not None and record.context_ref != context_ref:
                    raise RuntimeError(
                        f"artifact context differs from snapshot: "
                        f"{relative}/{record.line_index}"
                    )
            if count == 0:
                raise RuntimeError(f"empty artifact file is not canonical: {relative}")
            files += 1
    finally:
        connection.close()
    if files == 0 or records == 0:
        raise RuntimeError("restored artifact store contains no link records")
    return files, records


def seed(repo: Path, snapshot: Path, engine_components_output: Path) -> tuple[int, int]:
    repo = repo.resolve()
    snapshot = snapshot.resolve()
    baseline, baseline_fingerprint = _read_baseline_file(str(repo / "baseline"))
    if not baseline or baseline_fingerprint is None:
        raise RuntimeError("committed book baseline/fingerprint is missing")
    meta = _load_json(repo / "meta.json")
    try:
        meta_snapshot = meta["snapshot"]["sha256"]
        meta_count = meta["snapshot"]["book_count"]
        meta_fingerprint = meta["engine"]["fingerprint"]
    except (KeyError, TypeError) as error:
        raise RuntimeError("committed meta.json lacks line-baseline lineage") from error
    actual_snapshot = sha256_of_file(str(snapshot))
    if actual_snapshot != meta_snapshot:
        raise RuntimeError(
            f"seed snapshot differs from committed lineage: "
            f"{actual_snapshot} != {meta_snapshot}"
        )
    current = snapshot_book_hashes(str(snapshot))
    if current != baseline or meta_count != len(baseline):
        raise RuntimeError("seed snapshot differs from the committed per-book baseline")
    if meta_fingerprint != baseline_fingerprint:
        raise RuntimeError("meta and book baseline engine fingerprints differ")
    components = _engine_components(baseline_fingerprint)
    artifact_files, artifact_records = _validate_artifacts(repo, snapshot, baseline)
    build_line_baseline(
        str(snapshot),
        str(repo / DIRECTORY_NAME),
        current_hashes=current,
        snapshot_sha256=actual_snapshot,
        engine_fingerprint=baseline_fingerprint,
        artifacts_root=str(repo / "artifacts"),
    )
    engine_components_output.parent.mkdir(parents=True, exist_ok=True)
    temporary = engine_components_output.with_name(
        engine_components_output.name + f".tmp-{os.getpid()}"
    )
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write("".join(f"{item}\n" for item in components))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, engine_components_output)
    return artifact_files, artifact_records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--engine-components-output", required=True, type=Path)
    args = parser.parse_args()
    files, records = seed(args.repo, args.snapshot, args.engine_components_output)
    print(
        f"seeded exact line baseline after validating "
        f"{files} artifact files / {records} records",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
