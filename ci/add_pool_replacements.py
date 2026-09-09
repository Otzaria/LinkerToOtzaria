#!/usr/bin/env python3
"""Add the resolver pool's OWN child replacements to relink_work.json.

WHY.  `workers_replaced` counts the bounded replacements THIS DRIVER made
(src/incremental.py `_run_engine`), and on the production `--engine-pool` path the
driver supervises exactly one process: the pool master.  A child that dies with a
nonzero status or is killed by the stall watchdog is replaced by the master itself
(src/link_books.py `run_pool`), which keeps that count in local state and never
returns it.  So the release published `workers_replaced: 0` for precisely the
population the counter exists to surface — the 24 and 12 children the 2026-09-06
relinks lost would all have been reported as zero.

WHERE THE NUMBER COMES FROM.  `run_pool` already writes
`<run_dir>/logs/pool-exit-codes.json` in its finally block: `{label: [exit code of
each non-recycle life]}`.  Every life the SUPERVISION LOOP reaps appends exactly one
code, and a label has one life at start-up plus one per bounded replacement, so

    replacements(label) = max(0, len(codes[label]) - 1)

is EXACT, not an estimate.  A recycle (RECYCLE_EXIT_CODE) re-forks WITHOUT appending,
which is right: a recycle is not a replacement.  A fork that fails appends 1 in place
of the life it could not start, so the identity holds on that path too.  SIGTERM keeps
it too: the loop keeps reaping (and appending) until every child is gone.

WHAT IT DOES NOT COVER — AND WHY THOSE CASES PUBLISH null.  A MemoryError requeue is a
recycle, and is published under `books_requeued_after_memoryerror` /
`workers_recycled_for_heavy`.  Two ledger states are NOT a number at all, and this
script refuses to dress either of them up as one:

* MORE THAN ONE MASTER RAN.  `run_pool` writes the ledger with mode "w" at a fixed
  path, so a replacement master TRUNCATES its predecessor's ledger and the children
  that one had already replaced are gone.  The driver replaces the master on the pool
  path up to `--engine-restart-limit` times (relink.yml passes 2), so up to three
  masters can share one run dir, and the surviving ledger covers only the last.  The
  tell is co-published: `workers_replaced` IS the driver's count of master
  replacements, so `workers_replaced != 0` means exactly "a later master overwrote an
  earlier ledger".  In that case the honest answer is `null`, not the last master's
  number.  We deliberately retain that ``null`` rather than add an incomplete
  per-master ledger here: a master can be SIGKILLed before its ``finally`` writes any
  file, so filenames alone cannot prove a complete total.  Exact multi-master
  accounting needs a separately-versioned launch manifest plus atomic per-life
  journal writes; until that protocol exists, this producer must not overstate a
  partial sum.
* NO WORKER LIFE WAS EVER ACCOUNTED.  `codes` is pre-seeded `{label: []}` and a code is
  appended only where the SUPERVISION LOOP reaps a life (or a fork fails); the
  `finally` drain reaps WITHOUT appending.  So an empty list means "no life of this
  label was ever accounted for", not "this label never needed replacing" — a master
  that left the loop by an exception drains its children silently.  All labels empty is
  therefore an unaccounted ledger, published as `null`.  SOME labels empty keeps the
  number derived from the rest (it is an undercount of at most one per empty label,
  never an overcount) and NAMES the empty labels in the step line.

Both of those are reachable with `workers_replaced == 0`: the driver accepts a nonzero
master without replacing it when every book already carries a `done` marker
(src/incremental.py `_run_engine`), so the "`workers_replaced >= 1` will say so"
argument does not hold on that path, which is why the empty-ledger case is checked on
its own.

FAIL-OPEN BY DESIGN.  This annotates a release asset; it must never be the reason a
successful relink fails to publish.  No ledger (every non-pool path, an empty delta,
a master killed before its finally block), an unusable ledger, a truncated one and an
unaccounted one all report `pool_workers_replaced: null` — "the pool's own exit ledger
could not answer" — and say so in one line on stdout.  `null` is what the schema and
`--require-pool-accounting` already accept as unknown (ci/validate_relink_work.py: the
key must be PRESENT, and null or a non-negative int), so publishing it costs no gate.
Only relink_work.json itself being malformed is fatal, because that means the object
is not what the schema says it is.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

POOL_KEY = "pool_workers_replaced"


def pool_replacements(codes) -> int:
    """sum(max(0, lives - 1)) over the labels run_pool recorded."""
    if not isinstance(codes, dict) or not codes:
        raise ValueError("not a non-empty {label: [exit codes]} object")
    total = 0
    for label, lives in codes.items():
        if not isinstance(lives, list) or not all(type(code) is int for code in lives):
            raise ValueError(f"{label!r} does not hold a list of integer exit codes")
        total += max(0, len(lives) - 1)
    return total


def unaccounted_labels(codes) -> list[str]:
    """The labels whose list is empty: no life of theirs was ever accounted for."""
    return sorted(label for label, lives in codes.items() if not lives)


def driver_master_replacements(value) -> int | None:
    """The driver's own count of pool-MASTER replacements, or None if it states none.

    Anything but a non-negative int means the work object is not what the schema says;
    ci/validate_relink_work.py fails on it right after this script, but until then the
    number of masters is unknown, and unknown is exactly what must not be published as
    a count.
    """
    replaced = value.get("workers_replaced")
    return replaced if type(replaced) is int and replaced >= 0 else None


def read_pool_replacements(path: Path, driver_replaced: int | None = 0) -> tuple[int | None, str]:
    """(replacements, the one line for the step log).  None wherever the ledger cannot
    answer the question: no ledger, an unusable one, a ledger a later master truncated
    (``driver_replaced`` nonzero or unknown), or one that accounted no worker life."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, (f"pool accounting: pool_workers_replaced=null (no ledger at {path}: "
                      "no --engine-pool, or the master never wrote one)")
    except OSError as error:
        return None, (f"::warning::pool accounting: pool_workers_replaced=null "
                      f"({path} cannot be read: {error})")
    try:
        codes = json.loads(raw.decode("utf-8"))
        total = pool_replacements(codes)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return None, (f"::warning::pool accounting: pool_workers_replaced=null "
                      f"({path} is unusable: {error})")
    if driver_replaced is None:
        return None, (f"::warning::pool accounting: pool_workers_replaced=null "
                      f"(the work object states no usable workers_replaced, so it is "
                      f"unknown how many pool masters ran; {path} covers only the last one)")
    if driver_replaced:
        return None, (f"::warning::pool accounting: pool_workers_replaced=null "
                      f"(the driver replaced the pool master {driver_replaced}x, so "
                      f"{driver_replaced + 1} pool masters ran; the ledger only covers "
                      f"the last one — each master truncates {path}, so the children "
                      f"the earlier masters replaced are not in it)")
    unaccounted = unaccounted_labels(codes)
    if len(unaccounted) == len(codes):
        return None, (f"::warning::pool accounting: pool_workers_replaced=null "
                      f"({path} recorded no worker life at all "
                      f"({', '.join(unaccounted)}); the master did not reach its "
                      f"supervision loop, and its children were drained unaccounted)")
    line = (f"pool accounting: pool_workers_replaced={total} "
            f"({len(codes)} worker slot(s) in {path}")
    if unaccounted:
        line = ("::warning::" + line +
                f"; {', '.join(unaccounted)} recorded no life, so this is a lower bound)")
    else:
        line += ")"
    return total, line


def load(path: Path) -> dict:
    """The driver's object, held to the same canonical bytes the validator demands.

    Restated here rather than imported so the annotation cannot LAUNDER a file the
    workflow reshaped: this script rewrites relink_work.json, and it runs before
    ci/validate_relink_work.py, so a non-canonical producer file must die here or the
    canonical-bytes guard would never fire again.
    """
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate work key {key!r}")
            result[key] = value
        return result

    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict) or "workers_replaced" not in value:
        raise ValueError("relink_work is not the driver's work object")
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode() + b"\n"
    if raw != canonical:
        raise ValueError("relink_work is not canonical JSON with one trailing LF")
    return value


def annotate(work_path: Path, pool_path: Path) -> str:
    """Set POOL_KEY from the pool's ledger and rewrite the object.  Idempotent: the
    value is derived from the ledger every time, never accumulated."""
    value = load(work_path)
    # The driver's own count decides whether this ledger is the ONLY master's: a
    # nonzero workers_replaced means a later master truncated an earlier ledger.
    total, line = read_pool_replacements(pool_path, driver_master_replacements(value))
    value[POOL_KEY] = total
    work_path.write_bytes(
        json.dumps(value, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode() + b"\n"
    )
    return line


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("work", type=Path, help="relink_work.json, rewritten in place")
    parser.add_argument("pool_exit_codes", type=Path,
                        help="<run_dir>/logs/pool-exit-codes.json; absence is normal")
    args = parser.parse_args()
    print(annotate(args.work, args.pool_exit_codes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
