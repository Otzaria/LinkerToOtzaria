#!/usr/bin/env python3
"""Strict boundary validator for the compute → publisher relink handoff."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


KEYS = {
    "schema_version", "sefaria_tag", "snapshot_zst_sha256", "engine_fingerprint",
    "payload_sha256", "linker_commit", "relink_run_id", "relink_run_attempt",
    "relink_request_id", "parent_run_id", "parent_run_attempt",
}


def load(path: Path) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate manifest key {key!r}")
            result[key] = value
        return result

    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if raw != canonical:
        raise ValueError("manifest is not canonical JSON with one trailing LF")
    if not isinstance(value, dict) or set(value) != KEYS:
        raise ValueError("manifest does not match schema-v2 exact key set")
    return value


def validate(value: dict, args) -> None:
    if type(value["schema_version"]) is not int or value["schema_version"] != 2:
        raise ValueError("unsupported manifest schema")
    for field in ("snapshot_zst_sha256", "payload_sha256", "relink_request_id"):
        if type(value[field]) is not str or not re.fullmatch(r"[0-9a-f]{64}", value[field]):
            raise ValueError(f"invalid {field}")
    if type(value["linker_commit"]) is not str or not re.fullmatch(r"[0-9a-f]{40}", value["linker_commit"]):
        raise ValueError("invalid linker_commit")
    if type(value["sefaria_tag"]) is not str or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", value["sefaria_tag"]):
        raise ValueError("invalid sefaria_tag")
    fingerprint = value["engine_fingerprint"]
    if type(fingerprint) is not str or not (1 <= len(fingerprint) <= 4096) or not re.fullmatch(r"[ -~]+", fingerprint):
        raise ValueError("engine_fingerprint must be bounded printable ASCII")
    for field in ("relink_run_id", "relink_run_attempt"):
        if type(value[field]) is not int or value[field] < 1:
            raise ValueError(f"invalid {field}")
    for field in ("parent_run_id", "parent_run_attempt"):
        if type(value[field]) is not int or value[field] < 0:
            raise ValueError(f"invalid {field}")
    expected = {
        "payload_sha256": args.payload_sha256,
        "linker_commit": args.linker_commit,
        "relink_run_id": args.run_id,
        "relink_run_attempt": args.run_attempt,
        "relink_request_id": args.request_id,
        "parent_run_id": args.parent_run_id,
        "parent_run_attempt": args.parent_run_attempt,
    }
    for key, expected_value in expected.items():
        if value[key] != expected_value:
            raise ValueError(f"manifest {key} differs from workflow identity")
    if (args.parent_run_id == 0) != (args.parent_run_attempt == 0):
        raise ValueError("expected parent identity is half-empty")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--payload-sha256", required=True)
    parser.add_argument("--linker-commit", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--parent-run-id", type=int, default=0)
    parser.add_argument("--parent-run-attempt", type=int, default=0)
    args = parser.parse_args()
    validate(load(args.manifest), args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
