#!/usr/bin/env python3
"""Replay recorded production NER against a real, production-patched Sefaria resolver.

Run with Sefaria's venv, from its project directory (PYTHONPATH=.); Mongo must
contain the pinned dump. No GPU service or live NER request is used.
"""
import argparse
import json
from pathlib import Path
import sys
import os

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, default=ROOT / 'tests/fixtures/citation_cases_v36.json')
    parser.add_argument('--code-dir', type=Path, default=ROOT / 'src')
    args = parser.parse_args()
    sys.path.insert(0, str(args.code_dir.resolve()))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'sefaria.settings')
    import django
    django.setup()
    import link_books as engine
    from sefaria.model import Ref, library

    data = json.loads(args.fixture.read_text())
    assert data['schema_version'] == 1
    engine.quiet_sefaria_linker_logs()
    linker = library.get_linker('he')
    recognizer = linker.get_ner()
    cache = {}
    for row in data['cases']:
        normalized = recognizer._normalize_input([row['content']])[0]
        assert normalized == row['normalized'], (row['title'], row['index'], 'normalizer drift')
        if normalized in cache:
            assert cache[normalized] == row['ner'], 'conflicting recorded NER'
        cache[normalized] = row['ner']
    recognizer._bulk_recognize_entities_api_request = lambda texts: {
        'results': [cache[text] for text in texts]}
    count = 0
    for row in data['cases']:
        batch = [(row['index'], row['content'], row['context'])]
        records, _ = engine.process_batch(
            linker, engine.BookKey(row['source'], row['title']), batch,
            lambda message: (_ for _ in ()).throw(AssertionError(message)),
            context_ref_factory=Ref)
        actual, seen = set(), set()
        encoded = row['content'].encode('utf-16-le')
        for record in records:
            assert 0 <= record.start < record.end <= len(encoded) // 2
            anchor = encoded[2 * record.start:2 * record.end].decode('utf-16-le')
            assert record.source_hash == engine.content_hash(row['content'])
            assert Ref(record.target_ref).normal() == record.target_ref
            assert engine.target_has_text(Ref(record.target_ref))
            key = record.start, record.end
            assert key not in seen, (row['title'], row['index'], 'duplicate anchor', key)
            seen.add(key)
            actual.add((anchor, record.target_ref))
            assert record.target_ref not in row['forbidden_targets'], record.to_dict()
        for expected in row['expected']:
            assert (expected['anchor'], expected['target_ref']) in actual, (
                row['title'], row['index'], 'missing expected citation', expected, sorted(actual))
        count += len(records)
        print(f"PASS {row['title']}:{row['index']} ({len(records)} links)", flush=True)
    print(f"PASS {len(data['cases'])} recorded source lines, {count} valid links", flush=True)


if __name__ == '__main__':
    main()
