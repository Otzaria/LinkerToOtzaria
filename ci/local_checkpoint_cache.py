#!/usr/bin/env python3
"""Persist and restore an identity-bound local Linker batch checkpoint.

The cache lives on the durable self-hosted runner, outside the Actions checkout.
Only immutable per-batch JSONL shards and the exact changed-book plan are kept;
claim/done/failure ledgers are deliberately excluded and rebuilt on every run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time


HEX64 = re.compile(r"[0-9a-f]{64}\Z")
SHARD_PATH = re.compile(r"checkpoints/[0-9a-f]{40}/[0-9]{12}\.jsonl\Z")
IDENTITY_FIELDS = (
    "request_id",
    "parent_run_id",
    "parent_run_attempt",
    "snapshot_sha256",
    "sefaria_tag",
    "sefaria_metadata_sha256",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cache_path(args: argparse.Namespace) -> Path:
    if not HEX64.fullmatch(args.request_id):
        raise RuntimeError("request id must be 64 lowercase hexadecimal characters")
    if args.source_run_id <= 0 or args.source_run_attempt <= 0:
        raise RuntimeError("source run id/attempt must be positive")
    return (
        Path(args.cache_root)
        / args.request_id
        / f"source-{args.source_run_id}-{args.source_run_attempt}"
    )


def expected_identity(args: argparse.Namespace) -> dict:
    if not HEX64.fullmatch(args.snapshot_sha256):
        raise RuntimeError("snapshot sha256 must be 64 lowercase hexadecimal characters")
    if not HEX64.fullmatch(args.sefaria_metadata_sha256):
        raise RuntimeError("Sefaria metadata sha256 must be 64 lowercase hexadecimal characters")
    if args.parent_run_id <= 0 or args.parent_run_attempt <= 0:
        raise RuntimeError("parent run id/attempt must be positive")
    return {
        "request_id": args.request_id,
        "parent_run_id": args.parent_run_id,
        "parent_run_attempt": args.parent_run_attempt,
        "snapshot_sha256": args.snapshot_sha256,
        "sefaria_tag": args.sefaria_tag,
        "sefaria_metadata_sha256": args.sefaria_metadata_sha256,
    }


def listed_files(root: Path) -> list[dict]:
    files = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative == "checkpoint_manifest.json":
            continue
        if relative != "changed_books.json" and not SHARD_PATH.fullmatch(relative):
            raise RuntimeError(f"unsafe/unexpected checkpoint member: {relative}")
        if path.is_symlink():
            raise RuntimeError(f"checkpoint member must not be a symlink: {relative}")
        files.append({
            "path": relative,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    if not any(item["path"] == "changed_books.json" for item in files):
        raise RuntimeError("checkpoint lacks changed_books.json")
    return files


def save(args: argparse.Namespace) -> None:
    source = Path(args.run_dir)
    changed_books = source / "changed_books.json"
    checkpoints = source / "checkpoints"
    if not changed_books.is_file() or not checkpoints.is_dir():
        raise RuntimeError("run directory lacks a resumable plan/checkpoints directory")
    destination = cache_path(args)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{destination.name}.", dir=destination.parent
    ) as temporary_name:
        temporary = Path(temporary_name)
        shutil.copy2(changed_books, temporary / "changed_books.json")
        shutil.copytree(checkpoints, temporary / "checkpoints")
        manifest = {
            "schema_version": 1,
            "source_run_id": args.source_run_id,
            "source_run_attempt": args.source_run_attempt,
            **expected_identity(args),
            "created_unix": int(time.time()),
            "files": listed_files(temporary),
        }
        (temporary / "checkpoint_manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        replacement = destination.with_name(destination.name + ".replacement")
        shutil.rmtree(replacement, ignore_errors=True)
        os.replace(temporary, replacement)
        previous = destination.with_name(destination.name + ".previous")
        shutil.rmtree(previous, ignore_errors=True)
        if destination.exists():
            os.replace(destination, previous)
        os.replace(replacement, destination)
    print(f"saved local checkpoint: {destination} ({len(manifest['files']) - 1} shard files)")


def restore(args: argparse.Namespace) -> None:
    source = cache_path(args)
    manifest_path = source / "checkpoint_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid local checkpoint manifest: {error}") from error
    if manifest.get("schema_version") != 1:
        raise RuntimeError("unsupported local checkpoint manifest schema")
    if manifest.get("source_run_id") != args.source_run_id:
        raise RuntimeError("local checkpoint source run id mismatch")
    if manifest.get("source_run_attempt") != args.source_run_attempt:
        raise RuntimeError("local checkpoint source attempt mismatch")
    expected = expected_identity(args)
    for field in IDENTITY_FIELDS:
        if manifest.get(field) != expected[field]:
            raise RuntimeError(f"local checkpoint {field} mismatch")
    actual_files = listed_files(source)
    if manifest.get("files") != actual_files:
        raise RuntimeError("local checkpoint file set/size/digest mismatch")

    destination = Path(args.run_dir)
    destination.mkdir(parents=True, exist_ok=True)
    restored_checkpoints = destination / "checkpoints"
    temporary_checkpoints = destination / ".checkpoints.restore"
    shutil.rmtree(temporary_checkpoints, ignore_errors=True)
    shutil.copytree(source / "checkpoints", temporary_checkpoints)
    shutil.rmtree(restored_checkpoints, ignore_errors=True)
    os.replace(temporary_checkpoints, restored_checkpoints)
    temporary_plan = destination / ".changed_books.restore.json"
    shutil.copy2(source / "changed_books.json", temporary_plan)
    os.replace(temporary_plan, destination / "changed_books.json")
    print(f"restored exact local checkpoint: {source} ({len(actual_files) - 1} shard files)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("save", "restore"))
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--source-run-id", required=True, type=int)
    parser.add_argument("--source-run-attempt", required=True, type=int)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--parent-run-id", required=True, type=int)
    parser.add_argument("--parent-run-attempt", required=True, type=int)
    parser.add_argument("--snapshot-sha256", required=True)
    parser.add_argument("--sefaria-tag", required=True)
    parser.add_argument("--sefaria-metadata-sha256", required=True)
    args = parser.parse_args()
    (save if args.mode == "save" else restore)(args)


if __name__ == "__main__":
    main()
