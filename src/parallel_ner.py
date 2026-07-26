"""Plan and verify disjoint parallel raw-NER shards.

Kaggle kernels run only ``precompute_ner.py``.  This module splits one immutable
full-corpus plan into balanced, non-overlapping subplans and later merges their
content-addressed handoffs back into the exact full-plan order consumed by the
CPU resolver.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from itertools import zip_longest
from pathlib import Path

from link_books import book_lines, is_ner_eligible_line
from linker_artifact import BookKey
from line_baseline import indices_from_ranges
from ner_handoff import (
    NerBundle,
    load_json_strict,
    safe_relative_path,
    sha256_file,
    validate_book_key,
    validate_plan,
    write_json_atomic,
)


def _validated_plan(path: Path, snapshot: Path) -> dict:
    plan = load_json_strict(path)
    return validate_plan(
        plan,
        request_id=plan.get("relink_request_id"),
        snapshot_sha256=sha256_file(snapshot),
        engine_fingerprint=plan.get("engine_fingerprint"),
    )


def _book_key(item: dict) -> tuple[str, str]:
    return item["source_name"], item["canonical_he_title"]


def _workloads(snapshot: Path, changed: list[dict]) -> dict[tuple[str, str], dict]:
    connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    try:
        workloads = {}
        for item in changed:
            key = _book_key(item)
            allowed = indices_from_ranges(item["ner_ranges"])
            lines = book_lines(connection, BookKey(*key))
            eligible = [
                content
                for line_index, content, _context in lines
                if (
                    line_index in allowed
                    and is_ner_eligible_line(content)
                )
            ]
            workloads[key] = {
                "lines": len(eligible),
                "words": sum(len(content.split()) for content in eligible),
                "characters": sum(len(content) for content in eligible),
            }
        return workloads
    finally:
        connection.close()


def split_plan(snapshot: Path, plan_path: Path, output: Path, shard_count: int) -> dict:
    if shard_count < 2:
        raise RuntimeError("parallel NER requires at least two shards")
    plan = _validated_plan(plan_path, snapshot)
    changed = plan["changed"]
    if shard_count > len(changed):
        raise RuntimeError("shard count exceeds changed-book count")
    workloads = _workloads(snapshot, changed)

    # Longest-processing-time bin packing gives stable, well-balanced whole-book
    # shards without splitting a book or sharing mutable checkpoint state.
    bins = [{"words": 0, "characters": 0, "lines": 0, "books": []}
            for _ in range(shard_count)]
    indexed = {key: index for index, key in enumerate(map(_book_key, changed))}
    for item in sorted(
        changed,
        key=lambda value: (
            -workloads[_book_key(value)]["characters"],
            indexed[_book_key(value)],
        ),
    ):
        target = min(
            enumerate(bins),
            key=lambda pair: (pair[1]["characters"], pair[0]),
        )[1]
        work = workloads[_book_key(item)]
        target["books"].append(item)
        for field in ("words", "characters", "lines"):
            target[field] += work[field]

    output.mkdir(parents=True, exist_ok=True)
    descriptors = []
    for index, shard in enumerate(bins):
        shard["books"].sort(key=lambda item: indexed[_book_key(item)])
        document = dict(plan)
        document["changed"] = shard["books"]
        path = output / f"shard-{index:02d}.json"
        size, digest = write_json_atomic(path, document)
        descriptors.append({
            "index": index,
            "path": path.name,
            "size": size,
            "sha256": digest,
            "book_count": len(shard["books"]),
            "eligible_line_count": shard["lines"],
            "word_count": shard["words"],
            "character_count": shard["characters"],
        })

    full_keys = [_book_key(item) for item in changed]
    assigned = [
        _book_key(item)
        for shard in bins
        for item in shard["books"]
    ]
    if len(assigned) != len(set(assigned)) or set(assigned) != set(full_keys):
        raise RuntimeError("parallel shard plan is not an exact disjoint cover")
    manifest = {
        "schema_version": 1,
        "snapshot_sha256": sha256_file(snapshot),
        "full_plan_sha256": sha256_file(plan_path),
        "relink_request_id": plan["relink_request_id"],
        "engine_fingerprint": plan["engine_fingerprint"],
        "shard_count": shard_count,
        "book_count": len(changed),
        "eligible_line_count": sum(item["eligible_line_count"] for item in descriptors),
        "word_count": sum(item["word_count"] for item in descriptors),
        "character_count": sum(item["character_count"] for item in descriptors),
        "shards": descriptors,
    }
    write_json_atomic(output / "shard_manifest.json", manifest)
    return manifest


def merge_bundles(
    snapshot: Path,
    plan_path: Path,
    bundles: list[Path],
    output: Path,
    expected_batch_lines: int | None,
) -> int:
    plan = _validated_plan(plan_path, snapshot)
    full_changed = plan["changed"]
    full_by_key = {_book_key(item): item for item in full_changed}
    hashes = {
        _book_key(item): item["hash"]
        for item in plan["current_books"]
    }
    snapshot_digest = sha256_file(snapshot)
    descriptors = {}
    batch_lines = None
    output = output.resolve()
    if output.exists():
        shutil.rmtree(output)
    (output / "ner-data").mkdir(parents=True)

    for bundle in bundles:
        bundle = bundle.resolve()
        manifest = load_json_strict(bundle / "ner_manifest.json")
        raw_descriptors = manifest.get("books")
        if type(raw_descriptors) is not list:
            raise RuntimeError(f"{bundle}: invalid books array")
        keys = [
            validate_book_key(item.get("book"), f"{bundle}.books[{index}].book")
            for index, item in enumerate(raw_descriptors)
        ]
        if any(key not in full_by_key for key in keys):
            raise RuntimeError(f"{bundle}: contains a book outside the full plan")
        if any(key in descriptors for key in keys):
            raise RuntimeError(f"{bundle}: overlaps a prior NER shard")
        changed = [full_by_key[key] for key in keys]
        verified = NerBundle(
            bundle,
            request_id=plan["relink_request_id"],
            snapshot_sha256=snapshot_digest,
            engine_fingerprint=plan["engine_fingerprint"],
            changed_books=changed,
            expected_book_hashes=hashes,
            expected_batch_lines=expected_batch_lines,
        )
        if batch_lines is None:
            batch_lines = verified.batch_lines
        elif batch_lines != verified.batch_lines:
            raise RuntimeError("parallel NER shards use different batch sizes")
        for descriptor, key in zip(raw_descriptors, keys):
            relative = Path(safe_relative_path(
                descriptor["manifest_path"],
                f"{bundle}.manifest_path",
            ))
            source_dir = (bundle / relative).parent
            destination_dir = output / relative.parent
            if destination_dir.exists():
                raise RuntimeError(f"duplicate NER data directory for {key!r}")
            shutil.copytree(source_dir, destination_dir)
            descriptors[key] = descriptor

    expected_keys = [_book_key(item) for item in full_changed]
    missing = [key for key in expected_keys if key not in descriptors]
    if missing:
        raise RuntimeError(f"parallel NER handoff is incomplete: {len(missing)} book(s) missing")
    ordered = [descriptors[key] for key in expected_keys]
    write_json_atomic(output / "ner_manifest.json", {
        "schema_version": 2,
        "relink_request_id": plan["relink_request_id"],
        "snapshot_sha256": snapshot_digest,
        "engine_fingerprint": plan["engine_fingerprint"],
        "batch_lines": batch_lines,
        "books": ordered,
    })
    NerBundle(
        output,
        request_id=plan["relink_request_id"],
        snapshot_sha256=snapshot_digest,
        engine_fingerprint=plan["engine_fingerprint"],
        changed_books=full_changed,
        expected_book_hashes=hashes,
        expected_batch_lines=expected_batch_lines,
    )
    return len(ordered)


def _copy_or_link(source: str, destination: str) -> str:
    """Clone immutable NER data cheaply when both paths share a filesystem."""
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return destination


def _assert_exact_book_rows(
    old_snapshot: Path,
    new_snapshot: Path,
    keys: list[tuple[str, str]],
) -> None:
    """Prove that rebinding raw NER cannot change its normalized input."""
    old = sqlite3.connect(f"file:{old_snapshot}?mode=ro", uri=True)
    new = sqlite3.connect(f"file:{new_snapshot}?mode=ro", uri=True)
    query = (
        "SELECT line_index, content, context_ref FROM lines_snapshot "
        "WHERE source_name=? AND canonical_he_title=? ORDER BY line_index"
    )
    missing = object()
    try:
        for key in keys:
            old_rows = old.execute(query, key)
            new_rows = new.execute(query, key)
            for row_index, (old_row, new_row) in enumerate(
                zip_longest(old_rows, new_rows, fillvalue=missing)
            ):
                if old_row != new_row:
                    raise RuntimeError(
                        "cannot rebind NER: snapshot rows differ for "
                        f"{key!r} at row {row_index}"
                    )
    finally:
        old.close()
        new.close()


def rebind_bundle(
    old_snapshot: Path,
    old_plan_path: Path,
    bundle: Path,
    new_snapshot: Path,
    new_plan_path: Path,
    output: Path,
    expected_batch_lines: int | None,
) -> int:
    """Rebind a verified subset bundle after exact snapshot-row comparison.

    The raw recognizer output depends only on normalized input text, while the
    outer request/snapshot identities deliberately pin transport to one run.
    Rebinding is therefore permitted only when every source row, book hash,
    NER range and engine fingerprint matches exactly in both plans.
    """
    old_plan = _validated_plan(old_plan_path, old_snapshot)
    new_plan = _validated_plan(new_plan_path, new_snapshot)
    if old_plan["engine_fingerprint"] != new_plan["engine_fingerprint"]:
        raise RuntimeError("cannot rebind NER across engine fingerprints")

    bundle = bundle.resolve()
    manifest = load_json_strict(bundle / "ner_manifest.json")
    raw_descriptors = manifest.get("books")
    if type(raw_descriptors) is not list:
        raise RuntimeError(f"{bundle}: invalid books array")
    keys = [
        validate_book_key(item.get("book"), f"{bundle}.books[{index}].book")
        for index, item in enumerate(raw_descriptors)
    ]
    old_changed = {_book_key(item): item for item in old_plan["changed"]}
    new_changed = {_book_key(item): item for item in new_plan["changed"]}
    if any(key not in old_changed or key not in new_changed for key in keys):
        raise RuntimeError("cannot rebind NER outside both changed-book plans")

    old_hashes = {_book_key(item): item["hash"] for item in old_plan["current_books"]}
    new_hashes = {_book_key(item): item["hash"] for item in new_plan["current_books"]}
    for key in keys:
        if old_hashes.get(key) != new_hashes.get(key):
            raise RuntimeError(f"cannot rebind NER: source hash changed for {key!r}")
        if old_changed[key]["ner_ranges"] != new_changed[key]["ner_ranges"]:
            raise RuntimeError(f"cannot rebind NER: line plan changed for {key!r}")

    NerBundle(
        bundle,
        request_id=old_plan["relink_request_id"],
        snapshot_sha256=sha256_file(old_snapshot),
        engine_fingerprint=old_plan["engine_fingerprint"],
        changed_books=[old_changed[key] for key in keys],
        expected_book_hashes=old_hashes,
        expected_batch_lines=expected_batch_lines,
    )
    _assert_exact_book_rows(old_snapshot, new_snapshot, keys)

    output = output.resolve()
    if output.exists():
        shutil.rmtree(output)
    shutil.copytree(bundle, output, copy_function=_copy_or_link)
    rebound = dict(manifest)
    rebound["relink_request_id"] = new_plan["relink_request_id"]
    rebound["snapshot_sha256"] = sha256_file(new_snapshot)
    rebound["engine_fingerprint"] = new_plan["engine_fingerprint"]
    write_json_atomic(output / "ner_manifest.json", rebound)
    NerBundle(
        output,
        request_id=new_plan["relink_request_id"],
        snapshot_sha256=rebound["snapshot_sha256"],
        engine_fingerprint=new_plan["engine_fingerprint"],
        changed_books=[new_changed[key] for key in keys],
        expected_book_hashes=new_hashes,
        expected_batch_lines=expected_batch_lines,
    )
    return len(keys)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    split = subparsers.add_parser("split")
    split.add_argument("--snapshot", type=Path, required=True)
    split.add_argument("--plan", type=Path, required=True)
    split.add_argument("--output", type=Path, required=True)
    split.add_argument("--shards", type=int, required=True)
    merge = subparsers.add_parser("merge")
    merge.add_argument("--snapshot", type=Path, required=True)
    merge.add_argument("--plan", type=Path, required=True)
    merge.add_argument("--bundle", type=Path, action="append", required=True)
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--expected-batch-lines", type=int)
    rebind = subparsers.add_parser("rebind")
    rebind.add_argument("--old-snapshot", type=Path, required=True)
    rebind.add_argument("--old-plan", type=Path, required=True)
    rebind.add_argument("--bundle", type=Path, required=True)
    rebind.add_argument("--new-snapshot", type=Path, required=True)
    rebind.add_argument("--new-plan", type=Path, required=True)
    rebind.add_argument("--output", type=Path, required=True)
    rebind.add_argument("--expected-batch-lines", type=int)
    args = parser.parse_args()
    if args.command == "split":
        manifest = split_plan(args.snapshot, args.plan, args.output, args.shards)
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    elif args.command == "merge":
        count = merge_bundles(
            args.snapshot,
            args.plan,
            args.bundle,
            args.output,
            args.expected_batch_lines,
        )
        print(f"merged {count} book handoffs into {args.output}")
    else:
        count = rebind_bundle(
            args.old_snapshot,
            args.old_plan,
            args.bundle,
            args.new_snapshot,
            args.new_plan,
            args.output,
            args.expected_batch_lines,
        )
        print(f"rebound {count} verified book handoffs into {args.output}")


if __name__ == "__main__":
    main()
