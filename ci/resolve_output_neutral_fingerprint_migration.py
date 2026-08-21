#!/usr/bin/env python3
"""Resolve one reviewed, output-neutral linker fingerprint migration.

The release payload is the authoritative prior linker state.  A source-only
orchestration change may legitimately change ``engine_src`` before a new
payload can be published; this helper turns a narrowly committed review record
into the existing exact ``OLD::NEW`` adoption contract.  Unknown drift remains
an error in ``incremental.py`` rather than being silently adopted.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


FINGERPRINT = re.compile(r"[ -~]{1,4096}\Z")


def resolve(baseline_path: Path, actual: str, migrations_path: Path) -> str:
    if not FINGERPRINT.fullmatch(actual):
        raise RuntimeError("actual engine fingerprint is not bounded printable ASCII")
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        migrations = json.loads(migrations_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read fingerprint migration inputs: {error}") from error
    previous = baseline.get("engine_fingerprint") if type(baseline) is dict else None
    if not isinstance(previous, str) or not FINGERPRINT.fullmatch(previous):
        raise RuntimeError("baseline has no bounded printable engine fingerprint")
    if previous == actual:
        return ""
    if (
        type(migrations) is not dict
        or set(migrations) != {"schema_version", "migrations"}
        or migrations["schema_version"] != 1
        or type(migrations["migrations"]) is not list
    ):
        raise RuntimeError("invalid output-neutral fingerprint migration registry")
    matches = []
    for index, value in enumerate(migrations["migrations"]):
        if (
            type(value) is not dict
            or set(value) != {"from", "to", "review"}
            or not isinstance(value["from"], str)
            or not isinstance(value["to"], str)
            or not isinstance(value["review"], str)
            or not FINGERPRINT.fullmatch(value["from"])
            or not FINGERPRINT.fullmatch(value["to"])
            or not value["review"].strip()
        ):
            raise RuntimeError(f"invalid output-neutral migration at index {index}")
        if value["from"] == previous and value["to"] == actual:
            matches.append(value)
    if len(matches) > 1:
        raise RuntimeError("ambiguous output-neutral fingerprint migration")
    return "" if not matches else f"{previous}::{actual}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--actual", required=True)
    parser.add_argument(
        "--migrations",
        type=Path,
        default=Path("baseline/output_neutral_fingerprint_migrations.json"),
    )
    args = parser.parse_args()
    print(resolve(args.baseline, args.actual, args.migrations))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
