#!/usr/bin/env python3
"""Strict boundary validator for relink_work.json — the run's work provenance.

Deliberately standalone, exactly like ci/validate_relink_manifest.py: it restates the
schema instead of importing src/incremental.py, so a producer bug in the schema cannot
validate itself.  tests/test_relink_work.py binds the two by feeding this validator the
producer's real output.

WHAT THIS FILE IS FOR.  Recovery 34021656701 adopted 5,126 of 5,127 books from run
34016397157 and computed exactly one; nothing in the published release said so.  The
release now carries this object beside linker_links.zst / meta.json /
relink_manifest.json, and the content-addressed release tag covers it.

WHAT IT DOES NOT DUPLICATE.  It carries no commit and no digests: the release tag binds
it to the relink_manifest.json that holds all of those, and the publisher verifies both
against the same handoff bytes.  It DOES carry relink_run_id/relink_run_attempt, so the
asset answers "which run?" when downloaded alone — a second self-report of an identity
the manifest also holds, and therefore one that must not be able to drift.  It cannot:
every CI call site passes --run-id/--run-attempt, which requires these fields to be
present and EQUAL to the same two coordinates ci/validate_relink_manifest.py checks the
manifest against, in the same publisher step, over the same handoff bytes.

WHAT THE ci/ LAYER ADDS.  Schema 1 has exactly two accepted key sets: the driver's, and
the driver's plus pool_workers_replaced, which ci/add_pool_replacements.py derives at
the producer from the pool master's own exit ledger (see POOL_KEY).  The schema version
is NOT bumped for it: src/incremental.py owns WORK_SCHEMA_VERSION, and a ci/ script
rewriting that field would make two producers the authority on one number.  The
publisher passes --require-pool-accounting instead, which pins the released form.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


COUNTERS = (
    "books_total", "books_adopted", "books_computed", "books_failed", "links_total",
    "books_requeued_after_memoryerror", "books_reclaimed_after_worker_death",
    "duplicates", "workers_replaced", "workers_recycled_for_heavy",
)
KEYS = set(COUNTERS) | {
    "schema_version", "checkpoint_source", "checkpoint_source_run_id",
    "checkpoint_source_attempt", "generated_at", "relink_run_id", "relink_run_attempt",
}
# The one key the driver does not write.  workers_replaced counts the DRIVER's bounded
# replacements, and on the --engine-pool path the driver supervises one process (the
# master); the master's own replacement of a crashed or stall-killed child never
# reaches it.  ci/add_pool_replacements.py derives that count at the producer from
# <run_dir>/logs/pool-exit-codes.json and adds it here, so schema 1 has exactly TWO
# accepted key sets: the driver's, and the driver's plus this one.  Null means the
# pool's own exit ledger was not available (every non-pool path, and an unusable
# file); it is never silently 0, which would read as "the pool replaced nobody".
POOL_KEY = "pool_workers_replaced"
# ci/local_checkpoint_cache.py's own name for a restored checkpoint. Bounded and
# printable for the same reason engine_fingerprint is in the manifest validator: this
# string reaches a workflow log and an operator's terminal.
CHECKPOINT_SOURCE = re.compile(r"[ -~]{1,200}")
# The exact shape relink.yml produces: `date -u +%Y-%m-%dT%H:%M:%SZ`, the same string
# meta.json records. Pinned rather than "some ISO-8601-ish text" so the two cannot
# describe the same instant differently.
GENERATED_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def load(path: Path) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate work key {key!r}")
            result[key] = value
        return result

    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if raw != canonical:
        raise ValueError("relink_work is not canonical JSON with one trailing LF")
    if not isinstance(value, dict) or set(value) not in (KEYS, KEYS | {POOL_KEY}):
        raise ValueError("relink_work does not match schema-v1 exact key set")
    return value


def validate(value: dict, expect_run_id: int | None = None,
             expect_run_attempt: int | None = None,
             require_pool_accounting: bool = False) -> None:
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported relink_work schema")
    # `type(...) is not int` rather than isinstance: True is an int and would sail
    # through every bound below (the same trap tests/test_relink_manifest.py pins).
    for field in COUNTERS:
        if type(value[field]) is not int or value[field] < 0:
            raise ValueError(f"invalid {field}")
    if value["books_adopted"] + value["books_computed"] + value["books_failed"] != value["books_total"]:
        raise ValueError("books_adopted + books_computed + books_failed != books_total")
    source = value["checkpoint_source"]
    if source is not None and (type(source) is not str or not CHECKPOINT_SOURCE.fullmatch(source)):
        raise ValueError("checkpoint_source must be null or bounded printable ASCII")
    run_id = value["checkpoint_source_run_id"]
    attempt = value["checkpoint_source_attempt"]
    for field, parsed in (("checkpoint_source_run_id", run_id), ("checkpoint_source_attempt", attempt)):
        if parsed is not None and (type(parsed) is not int or parsed < 1):
            raise ValueError(f"invalid {field}")
    if (run_id is None) != (attempt is None):
        raise ValueError("checkpoint source coordinates are half-empty")
    # A named source whose name this build could not parse keeps the raw string and
    # reports no ids; ids without a name would mean they were invented.
    if run_id is not None and source is None:
        raise ValueError("checkpoint source coordinates without a checkpoint_source")
    generated_at = value["generated_at"]
    if generated_at is not None and (
        type(generated_at) is not str or not GENERATED_AT.fullmatch(generated_at)
    ):
        raise ValueError("generated_at must be null or YYYY-MM-DDTHH:MM:SSZ")
    # This run's own coordinates. Null together off a runner (an operator's local
    # driver run); required and exact wherever a caller states what they must be.
    relink_run_id = value["relink_run_id"]
    relink_run_attempt = value["relink_run_attempt"]
    for field, parsed in (("relink_run_id", relink_run_id),
                          ("relink_run_attempt", relink_run_attempt)):
        if parsed is not None and (type(parsed) is not int or parsed < 1):
            raise ValueError(f"invalid {field}")
    if (relink_run_id is None) != (relink_run_attempt is None):
        raise ValueError("relink run coordinates are half-empty")
    for field, actual, expected in (("relink_run_id", relink_run_id, expect_run_id),
                                    ("relink_run_attempt", relink_run_attempt, expect_run_attempt)):
        if expected is not None and actual != expected:
            raise ValueError(f"{field} is {actual!r}, expected {expected!r}")
    pool = value.get(POOL_KEY)
    if pool is not None and (type(pool) is not int or pool < 0):
        raise ValueError(f"invalid {POOL_KEY}")
    # Demanded wherever a RELEASE is being made: the annotation is added at the
    # producer, so a producer path that lost the step must not publish an asset that
    # is silently missing the pool's own accounting.
    if require_pool_accounting and POOL_KEY not in value:
        raise ValueError(f"{POOL_KEY} is absent; ci/add_pool_replacements.py did not run")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("work", type=Path)
    # Optional so the validator stays usable on a developer's machine; passed by every
    # CI call site, where they are exactly the coordinates the manifest is held to.
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--run-attempt", type=int, default=None)
    parser.add_argument("--require-pool-accounting", action="store_true",
                        help="the object must carry pool_workers_replaced (null allowed)")
    args = parser.parse_args()
    validate(load(args.work), args.run_id, args.run_attempt,
             args.require_pool_accounting)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
