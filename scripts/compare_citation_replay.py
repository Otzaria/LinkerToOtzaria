#!/usr/bin/env python3
"""Compare complete books with identical recorded NER and the real Sefaria resolver.

Run from the pinned, production-patched Sefaria project with PYTHONPATH=.
The gzip JSON input is a list of source/title/index/content/context/normalized/ner
rows. Output contains every difference with source context, exact record counts,
and validated canonical targets, source hashes, UTF-16 anchors and unique keys.
This is an audit report, not an automatic assertion that new links are correct.
Independent --shard N/M processes can share the output directory.
"""
import argparse
import collections
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time


def load_engine(name, path):
    source = path.read_bytes()
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # Bind evidence to the bytes actually executed, even if the caller replaces
    # a checkout file while a long replay is running. Do not trust an old pyc or
    # re-read the path later and stamp old output with a newer engine identity.
    exec(compile(source, str(path), 'exec'), module.__dict__)
    module._audit_source_sha256 = hashlib.sha256(source).hexdigest()
    module.quiet_sefaria_linker_logs()
    return module


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recorded-ner', required=True, type=Path)
    parser.add_argument('--baseline-code', required=True, type=Path)
    parser.add_argument('--candidate-code', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--published-book-hashes', type=Path,
                        help='snapshot_hashes.json; identify complete matching published books')
    parser.add_argument('--shard', default='0/1', help='zero-based N/M')
    args = parser.parse_args()
    shard, shards = map(int, args.shard.split('/'))
    if not 0 <= shard < shards:
        parser.error('expected 0 <= N < M')
    sys.path.insert(0, str(args.candidate_code.parent.resolve()))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'sefaria.settings')
    import django
    django.setup()
    from sefaria.model import Ref, library

    before = load_engine('audit_baseline', args.baseline_code)
    after = load_engine('audit_candidate', args.candidate_code)
    identity = dict(input_sha256=sha(args.recorded_ner),
                    baseline_sha256=before._audit_source_sha256,
                    candidate_sha256=after._audit_source_sha256)
    published = {}
    if args.published_book_hashes:
        identity['published_hashes_sha256'] = sha(args.published_book_hashes)
        published = {(b['source_name'], b['canonical_he_title']): b['hash']
                     for b in json.loads(args.published_book_hashes.read_text())['books']}
    rows = json.loads(gzip.decompress(args.recorded_ner.read_bytes()))
    books = collections.defaultdict(list)
    for row in rows:
        books[row['source'], row['title']].append(row)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    linker = library.get_linker('he')
    ner = linker.get_ner()
    cache = {}
    for i, ((source, title), source_rows) in enumerate(books.items()):
        if i % shards != shard:
            continue
        report_path = args.output_dir / f'{i:03d}-report.json'
        # Resume only a complete, exact input/engine comparison. Cached results
        # from a different revision must never silently certify the current code.
        if report_path.exists():
            report = json.loads(report_path.read_text())
            if report.get('identity') == identity:
                continue
        start = time.monotonic()
        cache.clear()
        lines = []
        source_digest = hashlib.sha1()
        for row in source_rows:
            normalized = ner._normalize_input([row['content']])[0]
            assert normalized == row['normalized'], (title, row['index'], 'normalizer drift')
            assert normalized not in cache or cache[normalized] == row['ner']
            cache[normalized] = row['ner']
            lines.append((row['index'], row['content'], row['context']))
            for value in (str(row['index']), row['content'] or '', row['context'] or ''):
                source_digest.update(value.encode('utf-8'))
                source_digest.update(b'\0')
        assert [li for li, _, _ in lines] == sorted({li for li, _, _ in lines})
        ner._bulk_recognize_entities_api_request = lambda texts: {'results': [cache[t] for t in texts]}
        lookup = {li: text for li, text, _ in lines}
        outputs = []
        for label, engine in (('before', before), ('after', after)):
            artifact = args.output_dir / f'{i:03d}-{label}.json'
            stamp = args.output_dir / f'{i:03d}-{label}-identity.json'
            cache_identity = dict(input_sha256=identity['input_sha256'], source=source, title=title,
                                  engine_sha256=identity['baseline_sha256' if label == 'before' else 'candidate_sha256'])
            def fail(message):
                raise AssertionError(message)
            cached = json.loads(stamp.read_text()) if stamp.exists() and artifact.exists() else {}
            if cached.get('identity') == cache_identity and cached.get('artifact_sha256') == sha(artifact):
                records = [engine.LinkRecord.from_dict(r['record']) for r in json.loads(artifact.read_text())]
            else:
                records, _ = engine.process_book(linker, engine.BookKey(source, title), lines,
                                                fail, lambda: None, context_ref_factory=Ref)
            keys, rendered = {}, []
            for record in records:
                data = record.to_dict()
                key = record.line_index, record.start, record.end
                raw = lookup[record.line_index].encode('utf-16-le')
                assert 0 <= record.start < record.end <= len(raw) // 2
                anchor = raw[2 * record.start:2 * record.end].decode('utf-16-le')
                assert record.source_hash == engine.content_hash(lookup[record.line_index])
                target = Ref(record.target_ref)
                assert target.normal() == record.target_ref and engine.target_has_text(target)
                assert key not in keys, (title, key, 'duplicate anchor')
                keys[key] = data
                rendered.append(dict(record=data, anchor=anchor))
            save(artifact, rendered)
            save(stamp, dict(identity=cache_identity, artifact_sha256=sha(artifact)))
            outputs.append(keys)
        old, new = outputs
        differences = []
        counts = collections.Counter(unchanged=0, added=0, removed=0, changed=0)
        for key in sorted(old.keys() | new.keys()):
            if key in old and key in new:
                kind = 'unchanged' if old[key] == new[key] else 'changed'
            else:
                kind = 'removed' if key in old else 'added'
            counts[kind] += 1
            if kind != 'unchanged':
                li, left, right = key
                text = lookup[li]
                differences.append(dict(kind=kind, line=li, source_text=text,
                    anchor=text.encode('utf-16-le')[left*2:right*2].decode('utf-16-le'),
                    before=old.get(key), after=new.get(key)))
        report = dict(identity=identity, source=source, title=title, lines=len(lines),
                      source_book_hash=source_digest.hexdigest()[:16],
                      matches_published_book=(source_digest.hexdigest()[:16] == published.get((source, title)))
                      if args.published_book_hashes else None,
                      before=len(old), after=len(new), counts=dict(counts), differences=differences,
                      seconds=round(time.monotonic() - start, 2))
        save(report_path, report)
        print(json.dumps({k: v for k, v in report.items() if k != 'differences'}, ensure_ascii=False), flush=True)
    print('REPLAY_COMPLETE', args.shard, flush=True)


if __name__ == '__main__':
    main()
