#!/usr/bin/env python3
"""Remove one completed request's durable local checkpoints and sweep stale ones."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import time

HEX64 = re.compile(r"[0-9a-f]{64}")
LOCK_PREFIX = ".save-"
LOCK_SUFFIX = ".lock"


def exact_child(parent: Path, request_id: str) -> Path:
    target = (parent / request_id).resolve()
    if target.parent != parent or target.name != request_id:
        raise RuntimeError("resolved local checkpoint target escaped its exact namespace")
    return target


def newest_mtime(path: Path) -> float:
    """Latest mtime anywhere under path; symlinks are not followed."""
    newest = path.lstat().st_mtime
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            try:
                newest = max(newest, os.lstat(os.path.join(dirpath, name)).st_mtime)
            except FileNotFoundError:
                pass
    return newest


def sweep_stale(root: Path, keep: str, max_age_days: float, now: float) -> list[Path]:
    """Remove other requests' checkpoints untouched for max_age_days."""
    cutoff = now - max_age_days * 86400
    removed = []
    for parent in (root / "raw-ner", root):
        if not parent.is_dir():
            continue
        for entry in sorted(parent.iterdir()):
            if entry.name == keep or not HEX64.fullmatch(entry.name) or entry.is_symlink():
                continue
            if not entry.is_dir() or newest_mtime(entry) >= cutoff:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry)
    # A save lock goes only with its checkpoint: a live saver may still hold it.
    for entry in sorted(root.glob(f"{LOCK_PREFIX}*{LOCK_SUFFIX}")):
        request = entry.name[len(LOCK_PREFIX):-len(LOCK_SUFFIX)]
        if request == keep or not HEX64.fullmatch(request) or not entry.is_file():
            continue
        if (root / request).exists() or entry.lstat().st_mtime >= cutoff:
            continue
        entry.unlink(missing_ok=True)
        removed.append(entry)
    return removed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument(
        "--drop-batch-checkpoint",
        action="store_true",
        help="also remove this request's batch checkpoint (only once its payload shipped)",
    )
    parser.add_argument(
        "--max-age-days",
        type=float,
        help="also remove other requests' checkpoints untouched for this many days",
    )
    args = parser.parse_args()
    if not HEX64.fullmatch(args.request_id):
        parser.error("--request-id must be 64 lowercase hexadecimal characters")
    if args.max_age_days is not None and args.max_age_days < 1:
        parser.error("--max-age-days must be at least 1")
    root = Path(args.cache_root)
    if not root.is_absolute() or root == Path("/"):
        parser.error("--cache-root must be a specific absolute directory")
    root = root.resolve()
    target = exact_child((root / "raw-ner").resolve(), args.request_id)
    shutil.rmtree(target, ignore_errors=True)
    print(f"removed completed local raw-NER checkpoint: {target}")
    if args.drop_batch_checkpoint:
        batch = exact_child(root, args.request_id)
        shutil.rmtree(batch, ignore_errors=True)
        (root / f"{LOCK_PREFIX}{args.request_id}{LOCK_SUFFIX}").unlink(missing_ok=True)
        print(f"removed shipped local batch checkpoint: {batch}")
    if args.max_age_days is not None:
        # Hygiene only: a sweep failure must never fail a successful relink.
        try:
            for path in sweep_stale(root, args.request_id, args.max_age_days, time.time()):
                print(f"removed stale local checkpoint: {path}")
        except OSError as error:
            print(f"::warning::stale local checkpoint sweep stopped: {error}")


if __name__ == "__main__":
    main()
