#!/usr/bin/env python3
"""Remove one completed request's durable raw-NER checkpoint safely."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--request-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{64}", args.request_id):
        parser.error("--request-id must be 64 lowercase hexadecimal characters")
    root = Path(args.cache_root)
    if not root.is_absolute() or root == Path("/"):
        parser.error("--cache-root must be a specific absolute directory")
    raw_root = (root / "raw-ner").resolve()
    target = (raw_root / args.request_id).resolve()
    if target.parent != raw_root or target.name != args.request_id:
        raise RuntimeError("resolved raw-NER cache target escaped its exact namespace")
    shutil.rmtree(target, ignore_errors=True)
    print(f"removed completed local raw-NER checkpoint: {target}")


if __name__ == "__main__":
    main()
