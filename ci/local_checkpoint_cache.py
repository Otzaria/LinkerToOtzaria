#!/usr/bin/env python3
"""Persist and restore an identity-bound local Linker batch checkpoint.

The cache lives on the durable self-hosted runner, outside the Actions checkout.
Immutable per-batch JSONL shards, the exact changed-book plan, and completed-book
outputs are kept.  Claim/failure ledgers are deliberately excluded.  A completed
book is resumable only after its atomic public artifact (or explicit no-artifact
state) and done marker agree, so cancellation cannot turn hours of finished work
back into an all-book replay.
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
COMPLETED_ARTIFACT_PATH = re.compile(r"completed_artifacts/[0-9a-f]{40}\.jsonl\Z")
CLAIM_ID = re.compile(r"[0-9a-f]{40}\Z")
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
        if (
            relative not in {"changed_books.json", "completed_books.json"}
            and not SHARD_PATH.fullmatch(relative)
            and not COMPLETED_ARTIFACT_PATH.fullmatch(relative)
        ):
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


def _claim_id(source_name: str, canonical_he_title: str) -> str:
    return hashlib.sha1(
        f"{source_name}\0{canonical_he_title}".encode("utf-8")
    ).hexdigest()


def _load_plan(path: Path) -> tuple[list[dict], dict[str, dict]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid changed-book plan: {error}") from error
    if not isinstance(value, list):
        raise RuntimeError("changed-book plan must be an array")
    by_claim = {}
    for index, item in enumerate(value):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("source_name"), str)
            or not item["source_name"]
            or not isinstance(item.get("canonical_he_title"), str)
            or not item["canonical_he_title"]
            or not isinstance(item.get("hash"), str)
        ):
            raise RuntimeError(f"changed-book plan entry {index} has an invalid identity")
        cid = _claim_id(item["source_name"], item["canonical_he_title"])
        if cid in by_claim:
            raise RuntimeError(f"changed-book plan contains duplicate claim id {cid}")
        by_claim[cid] = item
    return value, by_claim


def _artifact_relpath(source_name: str, canonical_he_title: str) -> Path:
    # Import the single authoritative path sanitizer instead of duplicating it in
    # the checkpoint protocol.  The workflow invokes this script from repo root.
    import sys
    source_root = Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(source_root))
    from linker_artifact import BookKey, book_key_to_relpath
    return Path(book_key_to_relpath(BookKey(source_name, canonical_he_title)))


def _snapshot_completed(source: Path, repo: Path, destination: Path) -> list[dict]:
    _plan, by_claim = _load_plan(source / "changed_books.json")
    done_root = source / "done"
    failed_root = source / "failed"
    done_ids = sorted(path.name for path in done_root.iterdir() if path.is_file()) \
        if done_root.is_dir() else []
    entries = []
    artifacts_root = destination / "completed_artifacts"
    for cid in done_ids:
        if not CLAIM_ID.fullmatch(cid):
            raise RuntimeError(f"unsafe done marker name: {cid}")
        if (failed_root / cid).exists():
            continue
        item = by_claim.get(cid)
        if item is None:
            raise RuntimeError(f"done marker {cid} is absent from the exact plan")
        relpath = _artifact_relpath(item["source_name"], item["canonical_he_title"])
        public_artifact = repo / relpath
        cached_name = None
        if public_artifact.is_file():
            cached_name = f"{cid}.jsonl"
            artifacts_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(public_artifact, artifacts_root / cached_name)
        entries.append({
            "claim_id": cid,
            "source_name": item["source_name"],
            "canonical_he_title": item["canonical_he_title"],
            "hash": item["hash"],
            "artifact": cached_name,
        })
    return entries


def save(args: argparse.Namespace) -> None:
    source = Path(args.run_dir)
    repo = Path(args.repo)
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
        completed = _snapshot_completed(source, repo, temporary)
        (temporary / "completed_books.json").write_text(
            json.dumps(completed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        manifest = {
            "schema_version": 2,
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
    shard_count = sum(SHARD_PATH.fullmatch(item["path"]) is not None for item in manifest["files"])
    print(
        f"saved local checkpoint: {destination} "
        f"({shard_count} shard files, {len(completed)} completed books)"
    )


def restore(args: argparse.Namespace) -> None:
    source = cache_path(args)
    manifest_path = source / "checkpoint_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid local checkpoint manifest: {error}") from error
    schema_version = manifest.get("schema_version")
    if schema_version not in (1, 2):
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
    shutil.rmtree(destination / "completed_artifacts", ignore_errors=True)
    completed_source = source / "completed_books.json"
    if schema_version == 2:
        if not completed_source.is_file():
            raise RuntimeError("schema-2 checkpoint lacks completed_books.json")
        shutil.copy2(completed_source, destination / "completed_books.json")
        if (source / "completed_artifacts").is_dir():
            shutil.copytree(source / "completed_artifacts", destination / "completed_artifacts")
    else:
        try:
            (destination / "completed_books.json").unlink()
        except FileNotFoundError:
            pass
    shard_count = sum(SHARD_PATH.fullmatch(item["path"]) is not None for item in actual_files)
    completed_count = 0
    if schema_version == 2:
        completed_count = len(json.loads(completed_source.read_text(encoding="utf-8")))
    print(
        f"restored exact local checkpoint: {source} "
        f"({shard_count} shard files, {completed_count} completed books)"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("save", "restore"))
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--repo", required=True)
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
