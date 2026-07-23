#!/usr/bin/env python3
"""Real pinned-Sefaria equivalence probe for live-NER vs raw-NER replay.

This is intentionally not part of the offline suite.  Run it on a prepared resolver
host with Mongo and the NER endpoint available.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sef-project", required=True)
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    sys.path.insert(0, str(repo / "src"))
    sys.path.insert(0, args.sef_project)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sefaria.settings")
    import django
    django.setup()

    from incremental import snapshot_book_hashes  # noqa: F401 (proves imports match engine)
    from linker_artifact import BookKey
    from link_books import process_batch
    from ner_handoff import NerBundle, sha256_bytes, sha256_file, write_json_atomic
    from sefaria.model import library

    contents = [
        "ככתוב בראשית א א ובפסוק ב.",
        "ושם ג נאמר דבר נוסף.",
        "<b>ועיין</b> שמות ב:ג; תהלים ט״ז ח.",
        "דברי רש״י על בראשית א׳:א׳ 😀 וסיום.",
    ]
    batch = list(enumerate(contents))
    book = BookKey("QA", "פיצול NER")
    live_linker = library.get_linker("he")
    recognizer = live_linker.get_ner()
    original_request = recognizer._bulk_recognize_entities_api_request
    captured = {}

    def capture(normalized):
        value = original_request(normalized)
        captured["normalized"] = list(normalized)
        captured["results"] = value["results"]
        return value

    recognizer._bulk_recognize_entities_api_request = capture
    live_records, live_words = process_batch(live_linker, book, batch, lambda line: None, batch_start=0)
    recognizer._bulk_recognize_entities_api_request = original_request
    if not captured:
        raise SystemExit("live path did not call the raw NER transport")

    request_id = "a" * 64
    snapshot_digest = "b" * 64
    fingerprint = "qa-real-equivalence"
    with tempfile.TemporaryDirectory() as temporary_name:
        root = Path(temporary_name)
        cid = "c" * 40
        batch_path = root / "ner-data" / cid / "000000000000.json"
        size, digest = write_json_atomic(batch_path, {
            "schema_version": 1,
            "book": book.to_dict(),
            "batch_start": 0,
            "lines": [
                {
                    "line_index": index,
                    "normalized_sha256": sha256_bytes(normalized.encode("utf-8")),
                    "result": result,
                }
                for index, normalized, result in zip(
                    range(len(contents)), captured["normalized"], captured["results"]
                )
            ],
        })
        book_path = root / "ner-data" / cid / "book_manifest.json"
        book_size, book_digest = write_json_atomic(book_path, {
            "schema_version": 1,
            "book": book.to_dict(),
            "source_book_hash": "1" * 16,
            "line_count": len(contents),
            "eligible_line_count": len(contents),
            "batches": [{
                "batch_start": 0,
                "path": f"ner-data/{cid}/{batch_path.name}",
                "size": size,
                "sha256": digest,
            }],
        })
        write_json_atomic(root / "ner_manifest.json", {
            "schema_version": 1,
            "relink_request_id": request_id,
            "snapshot_sha256": snapshot_digest,
            "engine_fingerprint": fingerprint,
            "batch_lines": 25,
            "books": [{
                "book": book.to_dict(),
                "manifest_path": f"ner-data/{cid}/{book_path.name}",
                "size": book_size,
                "sha256": book_digest,
            }],
        })
        bundle = NerBundle(
            root,
            request_id=request_id,
            snapshot_sha256=snapshot_digest,
            engine_fingerprint=fingerprint,
            changed_books=[book.to_dict()],
            expected_book_hashes={(book.source_name, book.canonical_he_title): "1" * 16},
            expected_batch_lines=25,
        )
        replay_linker = library.get_linker("he")
        replay_records, replay_words = process_batch(
            replay_linker, book, batch, lambda line: None, batch_start=0, precomputed=bundle,
        )
    live = [record.to_dict() for record in live_records]
    replay = [record.to_dict() for record in replay_records]
    if live_words != replay_words or live != replay:
        raise SystemExit(f"split changed output\nlive={live!r}\nreplay={replay!r}")
    print(f"REAL SPLIT EQUIVALENCE OK: words={live_words} links={len(live)}")


if __name__ == "__main__":
    main()
