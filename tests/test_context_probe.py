"""Ibid books whose edits cannot move the ibid history are re-NERed by line.

The 2026-10-26 relink re-NERed 4.85M lines because about 5,500 books changed only
their author line, and any "שם" in a book used to force a whole-book pass.
"""
import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

import link_books  # noqa: E402
import precompute_ner  # noqa: E402
from line_baseline import (  # noqa: E402
    build_line_baseline,
    compute_line_delta,
    ibid_seam_counts,
    indices_from_ranges,
    line_fingerprint,
    next_book_state,
    plan_changed_books,
)
from linker_artifact import BookKey, read_artifact  # noqa: E402
from ner_handoff import NerBundle, validate_plan  # noqa: E402

BOOK = BookKey("Sefaria", "ספר בדיקה")
KEY = (BOOK.source_name, BOOK.canonical_he_title)
CONTEXT = "ספר בדיקה"

# "|" marks a citation line for the fakes below; "שם" opens an ibid line.
OLD = [
    (0, "כותרת הספר", CONTEXT),
    (1, "מחבר ישן", CONTEXT),
    (2, "מקור|Genesis 1:1", CONTEXT),
    (3, "שם בפסוק", CONTEXT),
    (4, "טקסט רגיל", CONTEXT),
    (5, "מקור|Exodus 2:2", CONTEXT),
    (6, "שם עוד", CONTEXT),
]
OLD_ENTITIES = {0: False, 1: False, 2: True, 3: True, 4: False, 5: True, 6: True}


def _fingerprints(rows):
    return [(index, line_fingerprint(content, context)) for index, content, context in rows]


def _with_line(rows, index, content):
    return [(i, content if i == index else c, ctx) for i, c, ctx in rows]


# Two lines per transport batch, so seven lines already have three seams.
_BATCH = mock.patch.object(link_books, "BATCH_LINES", 2)


def setUpModule():
    _BATCH.start()


def tearDownModule():
    _BATCH.stop()


def _flags(rows, entities):
    return [entities[i] for i, _, _ in rows]


def _seams(rows, entities):
    return ibid_seam_counts(rows, _flags(rows, entities), link_books.transport_seams(rows))


def _delta(new, old_rows=OLD, entities=OLD_ENTITIES, book_context=True):
    known = entities is not None and None not in entities.values()
    return compute_line_delta(
        _fingerprints(old_rows), new, old_entities=entities, old_book_context=book_context,
        old_ibid_seams=_seams(old_rows, entities) if known else None,
        batch_seams=link_books.transport_seams,
    )


def _snapshot(path, rows, key=KEY):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE lines_snapshot(source_name TEXT, canonical_he_title TEXT, "
        "line_index INTEGER, content TEXT, context_ref TEXT)"
    )
    connection.executemany(
        "INSERT INTO lines_snapshot VALUES(?,?,?,?,?)",
        [(*key, index, content, context) for index, content, context in rows],
    )
    connection.commit()
    connection.close()


class ContextProbePlanTest(unittest.TestCase):
    def plan(self, new_rows, entities=OLD_ENTITIES, book_context=True, old_rows=OLD):
        return _delta(new_rows, old_rows, entities, book_context)

    def test_entity_free_edit_probes_only_the_changed_line(self):
        delta = self.plan(_with_line(OLD, 1, "מחבר חדש"))
        self.assertEqual(indices_from_ranges(delta.ner_ranges), {1})
        self.assertEqual(delta.context_ranges, ((0, 7),))
        self.assertEqual({new for _, new in delta.reuse}, {0, 2, 3, 4, 5, 6})

    def test_a_dropped_entity_line_needs_the_whole_book(self):
        delta = self.plan(_with_line(OLD, 2, "מקור|Genesis 9:9"))
        self.assertIsNone(delta.context_ranges)
        self.assertEqual(indices_from_ranges(delta.ner_ranges), set(range(7)))

    def test_reordered_entity_lines_need_the_whole_book(self):
        swapped = [(2, *OLD[5][1:]), (5, *OLD[2][1:])]
        new = [row for row in OLD if row[0] not in (2, 5)] + swapped
        delta = self.plan(sorted(new))
        self.assertIsNone(delta.context_ranges)
        self.assertEqual(delta.reuse, ())

    def test_unknown_flags_or_a_stateless_prior_book_need_the_whole_book(self):
        new = _with_line(OLD, 1, "מחבר חדש")
        for entities, book_context in (({**OLD_ENTITIES, 4: None}, True),
                                       (None, None), (OLD_ENTITIES, False)):
            with self.subTest(entities=entities, book_context=book_context):
                delta = self.plan(new, entities, book_context)
                self.assertIsNone(delta.context_ranges)
                self.assertEqual(delta.reuse, ())

    def test_deleting_an_entity_free_line_probes_nothing(self):
        longer = OLD + [(7, "שורה אחרונה", CONTEXT)]
        delta = self.plan(OLD, {**OLD_ENTITIES, 7: False}, old_rows=longer)
        self.assertEqual(delta.ner_ranges, ())
        self.assertEqual(delta.context_ranges, ((0, 7),))

    def test_a_shifted_transport_seam_needs_the_whole_book(self):
        # Without line 4 the seams fall between other entity lines.
        delta = self.plan([(i if i < 4 else i - 1, c, ctx) for i, c, ctx in OLD if i != 4])
        self.assertIsNone(delta.context_ranges)
        self.assertEqual(delta.reuse, ())

    def test_line_baseline_round_trip_keeps_flags_and_book_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "artifacts").mkdir()
            _snapshot(repo / "old.db", OLD)
            hashes = {KEY: "1" * 16}
            build_line_baseline(
                str(repo / "old.db"), str(repo / "line-baseline"),
                current_hashes=hashes, snapshot_sha256="2" * 64,
                engine_fingerprint="engine", artifacts_root=str(repo / "artifacts"),
                book_state=lambda book, rows: (
                    _flags(rows, OLD_ENTITIES), _seams(rows, OLD_ENTITIES),
                ),
            )
            _snapshot(repo / "new.db", _with_line(OLD, 1, "מחבר חדש"))
            plans, reused, ner = plan_changed_books(
                str(repo / "new.db"), [BOOK], baseline_root=str(repo / "line-baseline"),
                baseline_hashes=hashes, baseline_identity_valid=True,
                batch_seams=link_books.transport_seams,
            )
            self.assertEqual((reused, ner), (6, 1))
            self.assertEqual(plans[KEY].context_ranges, ((0, 7),))

            # A baseline written without flags keeps the whole-book pass.
            build_line_baseline(
                str(repo / "old.db"), str(repo / "line-baseline"),
                current_hashes=hashes, snapshot_sha256="2" * 64,
                engine_fingerprint="engine", artifacts_root=str(repo / "artifacts"),
            )
            plans, reused, ner = plan_changed_books(
                str(repo / "new.db"), [BOOK], baseline_root=str(repo / "line-baseline"),
                baseline_hashes=hashes, baseline_identity_valid=True,
                batch_seams=link_books.transport_seams,
            )
            self.assertEqual((reused, ner), (0, 7))

    def test_schema_2_book_files_are_still_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "artifacts").mkdir()
            rows = [(0, "אאא", CONTEXT), (1, "בבב", CONTEXT)]
            _snapshot(repo / "old.db", rows)
            hashes = {KEY: "1" * 16}
            build_line_baseline(
                str(repo / "old.db"), str(repo / "line-baseline"),
                current_hashes=hashes, snapshot_sha256="2" * 64,
                engine_fingerprint="engine", artifacts_root=str(repo / "artifacts"),
            )
            [path] = (repo / "line-baseline" / "books").iterdir()
            value = json.loads(path.read_text(encoding="utf-8"))
            value["schema_version"] = 2
            del value["book_context"], value["ibid_seams"]
            value["lines"] = [row[:2] for row in value["lines"]]
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            _snapshot(repo / "new.db", [(0, "אאא", CONTEXT), (1, "גגג", CONTEXT)])
            _plans, reused, ner = plan_changed_books(
                str(repo / "new.db"), [BOOK], baseline_root=str(repo / "line-baseline"),
                baseline_hashes=hashes, baseline_identity_valid=True,
            )
            self.assertEqual((reused, ner), (1, 1))


class _Bundle:
    def __init__(self, found, escalated=()):
        self.found = found
        self.escalated = set(escalated)

    def entity_lines(self, key):
        return self.found


class LineEntityFlagsTest(unittest.TestCase):
    def flags(self, rows, delta, bundle, old_entities=OLD_ENTITIES):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "artifacts").mkdir()
            _snapshot(repo / "old.db", OLD)
            build_line_baseline(
                str(repo / "old.db"), str(repo / "line-baseline"),
                current_hashes={KEY: "1" * 16}, snapshot_sha256="2" * 64,
                engine_fingerprint="engine", artifacts_root=str(repo / "artifacts"),
                book_state=lambda book, rows: (
                    _flags(rows, old_entities), _seams(rows, old_entities),
                ),
            )
            state = next_book_state(
                baseline_root=str(repo / "line-baseline"),
                baseline_hashes={KEY: "1" * 16},
                line_deltas={} if delta is None else {KEY: delta},
                bundle=bundle,
                ner_eligible=link_books.is_ner_eligible_line,
                batch_seams=link_books.transport_seams,
            )
            flags, self.seams = state(BOOK, rows)
            return flags

    def test_probe_takes_ner_flags_and_carries_the_rest(self):
        new = _with_line(OLD, 1, "מחבר חדש")
        delta = _delta(new)
        self.assertEqual(
            self.flags(new, delta, _Bundle({1: False})),
            [False, False, True, True, False, True, True],
        )

    def test_escalated_book_takes_every_flag_from_ner(self):
        new = _with_line(OLD, 1, "מחבר|Numbers 3:3")
        delta = _delta(new)
        found = {i: i in (1, 2, 3, 5, 6) for i in range(7)}
        self.assertEqual(
            self.flags(new, delta, _Bundle(found, escalated={KEY})),
            [found[i] for i in range(7)],
        )

    def test_unchanged_book_carries_and_live_ner_is_unknown(self):
        self.assertEqual(self.flags(OLD, None, None), [OLD_ENTITIES[i] for i in range(7)])
        new = _with_line(OLD, 1, "מחבר חדש")
        delta = _delta(new)
        self.assertEqual(self.flags(new, delta, None)[1], None)

    def test_ineligible_planned_line_has_no_entity_and_eligible_gap_fails(self):
        delta = _delta(_with_line(OLD, 1, "מחבר חדש"))
        new = _with_line(OLD, 1, "<h2>x</h2>")
        self.assertFalse(self.flags(new, delta, _Bundle({}))[1])
        new = _with_line(OLD, 1, "מחבר חדש")
        with self.assertRaises(RuntimeError):
            self.flags(new, delta, _Bundle({}))


class BookLinePlanTest(unittest.TestCase):
    NEW = _with_line(OLD, 1, "מחבר חדש")
    ITEM = {"ner_ranges": [[1, 2]], "reuse": [[0, 0], [2, 2]], "context_ranges": [[0, 7]]}

    def plan(self, bundle, item=None):
        return link_books.book_line_plan(BOOK, self.NEW, item or self.ITEM, bundle)

    def test_unescalated_probe_resolves_narrow_and_stateless(self):
        self.assertEqual(self.plan(_Bundle({1: False})), ({1}, [(0, 0), (2, 2)], False))

    def test_escalated_or_live_probe_resolves_the_whole_book(self):
        for bundle in (_Bundle({}, escalated={KEY}), None):
            with self.subTest(bundle=bundle):
                self.assertEqual(self.plan(bundle), (set(range(7)), [], True))

    def test_an_unescalated_probe_with_an_entity_fails(self):
        with self.assertRaisesRegex(RuntimeError, "not inert"):
            self.plan(_Bundle({1: True}))

    def test_a_probe_must_cover_the_whole_book(self):
        with self.assertRaisesRegex(RuntimeError, "does not cover"):
            self.plan(_Bundle({1: False}), {**self.ITEM, "context_ranges": [[0, 6]]})


class _Ref:
    def __init__(self, normal):
        self._normal = normal
        self.index = type("Index", (), {"title": normal.split(" ")[0]})()

    def is_empty(self):
        return False

    def is_range(self):
        return False

    def normal(self):
        return self._normal

    def __eq__(self, other):
        return isinstance(other, _Ref) and other._normal == self._normal


class _History:
    """The pinned Sefaria IbidHistory: three refs, three titles and a title map."""

    def __init__(self):
        self._last_refs = []
        self._last_titles = []
        self._title_ref_map = {}

    @property
    def last_refs(self):
        return self._last_refs

    @last_refs.setter
    def last_refs(self, ref):
        self._last_refs += [ref]
        if ref.index.title not in self._title_ref_map:
            self._last_titles += [ref.index.title]
        self._title_ref_map[ref.index.title] = ref
        if len(self._last_titles) > 3:
            del self._title_ref_map[self._last_titles.pop(0)]
        if len(self._last_refs) > 3:
            self._last_refs.pop(0)

    def get_ref_by_title(self, title):
        return self._title_ref_map.get(title)


class _Resolver:
    def __init__(self):
        self._ibid_history = _History()

    def reset_ibid_history(self):
        self._ibid_history = _History()


class _ResolvedRef:
    is_ambiguous = False
    resolved_raw_refs = []

    def __init__(self, ref):
        self.ref = ref
        self.raw_entity = type("Raw", (), {
            "span": type("Span", (), {"range": (0, 2)})(), "raw_ref_parts": [],
        })()


class _IbidLinker:
    """"|" cites a ref; "שם #Title" takes that title's ref, a bare "שם" the latest one."""

    def __init__(self):
        self._ref_resolver = _Resolver()

    def bulk_link(self, texts, book_context_refs=None, type_filter=None, **_):
        self._ref_resolver.reset_ibid_history()
        docs = []
        for text in texts:
            history = self._ref_resolver._ibid_history
            refs = []
            if "|" in text:
                refs = [_Ref(text.split("|")[1])]
            elif text.startswith("שם") and "#" in text:
                found = history.get_ref_by_title(text.split("#")[1])
                refs = [found] if found else []
            elif text.startswith("שם") and history.last_refs:
                refs = [_Ref(f"{history.last_refs[-1].normal()}:{len(history.last_refs)}")]
            for ref in refs:
                history.last_refs = ref
            docs.append(type("Doc", (), {"resolved_refs": [_ResolvedRef(r) for r in refs]})())
        return docs


def _link(directory, name, lines, *, prior=None, ner_indices=None, reuse=(), stateful):
    out = Path(directory) / f"{name}.jsonl"
    if prior is not None:
        shutil.copyfile(prior, out)
    link_books.process_book_checkpointed(
        _IbidLinker(), BOOK, lines, lambda _line: None, lambda: None,
        str(Path(directory) / f"{name}-checkpoints"), str(out), lambda *_: None,
        ner_indices=ner_indices, reuse=reuse, context_ref_factory=_Ref,
        ibid_context=link_books.IbidContext() if stateful else None,
        defer_when_heavy=False,
    )
    return [
        (r.line_index, r.start, r.end, r.target_ref) for r in read_artifact(str(out))
    ] if out.exists() else []


# The title map outlives the last three refs, but a seam keeps only those refs.
TITLES = [
    (0, "טקסט רגיל", CONTEXT),
    (1, "מקור|Berakhot 2a", CONTEXT),
    (2, "מקור|Shabbat 3a", CONTEXT),
    (3, "מקור|Shabbat 4a", CONTEXT),
    (4, "מקור|Shabbat 5a", CONTEXT),
    (5, "שם בגמרא #Berakhot", CONTEXT),
]
TITLE_ENTITIES = {0: False, 1: True, 2: True, 3: True, 4: True, 5: True}


class ContextProbeEquivalenceTest(unittest.TestCase):
    def probe_and_whole(self, new, old=OLD, entities=OLD_ENTITIES, *, plan=True):
        with tempfile.TemporaryDirectory() as temporary:
            _link(temporary, "old", old, stateful=True)
            delta = _delta(new, old, entities)
            self.assertEqual(delta.context_ranges is not None, plan)
            if not plan:
                # What a probe would have done had the planner allowed it.
                delta = compute_line_delta(
                    _fingerprints(old), new, old_entities=entities, old_book_context=True,
                    old_ibid_seams=[], batch_seams=lambda rows: [],
                )
            probe = _link(
                temporary, "probe", new, prior=Path(temporary) / "old.jsonl",
                ner_indices=indices_from_ranges(delta.ner_ranges), reuse=delta.reuse,
                stateful=False,
            )
            whole = _link(temporary, "whole", new, stateful=True)
            self.assertTrue(whole)
            return probe, whole

    def test_entity_free_edit_links_exactly_like_a_whole_book_pass(self):
        for new in (
            _with_line(OLD, 1, "מחבר חדש"),
            _with_line(OLD, 4, "טקסט אחר"),
            OLD + [(7, "שורה נוספת", CONTEXT)],
        ):
            with self.subTest(new=new):
                probe, whole = self.probe_and_whole(new)
                self.assertEqual(probe, whole)

    def test_plain_lines_before_the_first_entity_move_no_history(self):
        new = [(0, "פתיחה", CONTEXT), (1, "עוד פתיחה", CONTEXT)] + [
            (i + 2, c, ctx) for i, c, ctx in TITLES
        ]
        probe, whole = self.probe_and_whole(new, TITLES, TITLE_ENTITIES)
        self.assertEqual(probe, whole)

    def test_same_seams_keep_the_title_map_history(self):
        probe, whole = self.probe_and_whole(
            _with_line(TITLES, 0, "טקסט אחר"), TITLES, TITLE_ENTITIES,
        )
        self.assertEqual(probe, whole)
        self.assertIn((5, 0, 2, "Berakhot 2a"), whole)

    def test_a_shifted_seam_that_drops_a_title_is_never_probed(self):
        new = [(i - 1, c, ctx) for i, c, ctx in TITLES if i != 0]
        probe, whole = self.probe_and_whole(new, TITLES, TITLE_ENTITIES, plan=False)
        self.assertNotEqual(probe, whole)

    def test_a_new_citation_is_why_the_producer_escalates(self):
        probe, whole = self.probe_and_whole(_with_line(OLD, 1, "מחבר|Numbers 3:3"))
        self.assertNotEqual(probe, whole)


class _Recognizer:
    @staticmethod
    def _normalize_input(texts):
        return list(texts)


def _fake_ner(_url, texts):
    return [
        {"entities": [{"label": "citation", "range": [0, 2]}]
         if "|" in text or text.startswith("שם") else []}
        for text in texts
    ]


def _produce(test, new, partial_layout=None):
    """Run the producer over one book; return its plan delta, manifest and bundle root."""
    temporary = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, temporary)
    root = Path(temporary) / "bundle"
    for name in ("claims", "done", "failed", "partial", "ner-data"):
        (root / name).mkdir(parents=True)
    cid = link_books.claim_id(BOOK)
    (root / "claims" / cid).mkdir()
    _snapshot(Path(temporary) / "new.db", new)
    delta = _delta(new)
    args = argparse.Namespace(ner_url="http://ner", worker_label="w01")
    con = sqlite3.connect(Path(temporary) / "new.db")
    try:
        with mock.patch.object(precompute_ner, "_post_bulk", side_effect=_fake_ner), \
                mock.patch.object(precompute_ner, "BATCH_LINES", 2):
            if partial_layout is not None:
                (root / "partial" / cid).mkdir()
                precompute_ner._produce_layout(
                    args, _Recognizer(), BOOK, "1" * 16, link_books.book_lines(con, BOOK),
                    partial_layout, root, Path(temporary) / "hb", [],
                )
            precompute_ner._produce_book(
                args, _Recognizer(), con, BOOK, "1" * 16, delta.ner_ranges, root,
                Path(temporary) / "hb", delta.context_ranges,
            )
    finally:
        con.close()
    manifest = json.loads(
        (root / "ner-data" / cid / "book_manifest.json").read_text(encoding="utf-8")
    )
    return delta, manifest, root


class ProducerEscalationTest(unittest.TestCase):
    def produce(self, new, partial_layout=None):
        delta, manifest, _root = _produce(self, new, partial_layout)
        return delta, manifest

    def test_entity_free_probe_stays_narrow(self):
        delta, manifest = self.produce(_with_line(OLD, 1, "מחבר חדש"))
        self.assertEqual(manifest["ner_ranges"], [list(r) for r in delta.ner_ranges])
        self.assertEqual(manifest["eligible_line_count"], 1)

    def test_probe_with_an_entity_covers_the_whole_book(self):
        delta, manifest = self.produce(_with_line(OLD, 1, "מחבר|Numbers 3:3"))
        self.assertEqual(manifest["ner_ranges"], [list(r) for r in delta.context_ranges])
        self.assertEqual(manifest["eligible_line_count"], 7)

    def test_resumed_probe_still_escalates(self):
        new = _with_line(OLD, 1, "מחבר|Numbers 3:3")
        delta = _delta(new)
        _unused, manifest = self.produce(new, partial_layout=delta.ner_ranges)
        self.assertEqual(manifest["ner_ranges"], [list(r) for r in delta.context_ranges])


class HandoffContractTest(unittest.TestCase):
    def plan(self, context_ranges):
        item = {**BOOK.to_dict(), "ner_ranges": [[1, 2]]}
        if context_ranges is not None:
            item["context_ranges"] = context_ranges
        return {
            "schema_version": 3, "relink_request_id": "a" * 64,
            "snapshot_sha256": "b" * 64, "changelog_sha256": None,
            "engine_fingerprint": "engine", "changed": [item], "removed": [],
            "current_books": [{**BOOK.to_dict(), "hash": "1" * 16}],
        }

    def validate(self, context_ranges):
        return validate_plan(
            self.plan(context_ranges), request_id="a" * 64, snapshot_sha256="b" * 64,
            engine_fingerprint="engine",
        )

    def test_plan_accepts_only_a_strict_whole_book_superset(self):
        self.validate(None)
        self.validate([[0, 7]])
        for bad in ([[1, 2]], [[2, 7]], "all"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                self.validate(bad)

    def test_bundle_marks_escalation_and_reports_entities(self):
        for new, escalated in (
            (_with_line(OLD, 1, "מחבר חדש"), False),
            (_with_line(OLD, 1, "מחבר|Numbers 3:3"), True),
        ):
            with self.subTest(escalated=escalated):
                delta, _manifest, root = _produce(self, new)
                cid = link_books.claim_id(BOOK)
                manifest_path = root / "ner-data" / cid / "book_manifest.json"
                (root / "ner_manifest.json").write_text(json.dumps({
                    "schema_version": 3, "relink_request_id": "a" * 64,
                    "snapshot_sha256": "b" * 64, "engine_fingerprint": "engine",
                    "batch_lines": 2, "batch_chars": 120000,
                    "books": [{
                        "book": BOOK.to_dict(),
                        "manifest_path": f"ner-data/{cid}/book_manifest.json",
                        "size": manifest_path.stat().st_size,
                        "sha256": precompute_ner.sha256_file(manifest_path),
                    }],
                }, ensure_ascii=False), encoding="utf-8")
                bundle = NerBundle(
                    root, request_id="a" * 64, snapshot_sha256="b" * 64,
                    engine_fingerprint="engine",
                    changed_books=[{
                        **BOOK.to_dict(),
                        "ner_ranges": [list(r) for r in delta.ner_ranges],
                        "context_ranges": [list(r) for r in delta.context_ranges],
                    }],
                )
                self.assertEqual(KEY in bundle.escalated, escalated)
                found = bundle.entity_lines(KEY)
                self.assertEqual(any(found.values()), escalated)


class IncrementalContextProbeTest(unittest.TestCase):
    """Two real driver runs over a producer-built bundle and a stub engine."""

    REQUEST = "c" * 64

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        for name in ("repo/baseline", "repo/artifacts", "run"):
            (self.tmp / name).mkdir(parents=True)
        self.payloads = []
        engine = mock.patch("incremental._run_engine", side_effect=self.engine)
        engine.start()
        self.addCleanup(engine.stop)

    def engine(self, args, only_books_path, progress_seconds=60.0, stats=None):
        if stats is not None:
            stats["workers_replaced"] = 0
        payload = json.loads(Path(only_books_path).read_text(encoding="utf-8"))
        self.payloads.append(payload)
        (Path(args.run_dir) / "done").mkdir(exist_ok=True)
        for item in payload:
            book = BookKey(item["source_name"], item["canonical_he_title"])
            (Path(args.run_dir) / "done" / link_books.claim_id(book)).touch()
        return [0]

    def run_cycle(self, rows, name):
        import incremental
        snapshot = self.tmp / f"{name}.db"
        _snapshot(snapshot, rows)
        bundle = self.tmp / f"{name}-bundle"
        args = argparse.Namespace(
            repo=str(self.tmp / "repo"), snapshot=str(snapshot), changelog=None,
            sefaria_tag="t", generated_at="2026-10-26T00:00:00Z", run_dir=str(self.tmp / "run"),
            sef_project=str(self.tmp), python="python3", bavli_convention=False,
            engine_fingerprint="engine", relink_request_id=self.REQUEST,
            ner_bundle_dir=str(bundle), ner_url="http://ner", worker_label="w01",
        )
        plan_path = self.tmp / f"{name}-plan.json"
        incremental.write_incremental_plan(args, str(plan_path), self.REQUEST)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        for name_ in ("claims", "done", "failed", "partial", "ner-data"):
            (bundle / name_).mkdir(parents=True)
        con = sqlite3.connect(snapshot)
        try:
            with mock.patch.object(precompute_ner, "_post_bulk", side_effect=_fake_ner):
                for item in plan["changed"]:
                    book = BookKey(item["source_name"], item["canonical_he_title"])
                    (bundle / "claims" / link_books.claim_id(book)).mkdir()
                    precompute_ner._produce_book(
                        args, _Recognizer(), con, book, "1" * 16,
                        tuple(map(tuple, item["ner_ranges"])), bundle, self.tmp / "hb",
                        tuple(map(tuple, item["context_ranges"]))
                        if "context_ranges" in item else None,
                    )
        finally:
            con.close()
        precompute_ner.finalize(argparse.Namespace(
            plan=str(plan_path), snapshot=str(snapshot), relink_request_id=self.REQUEST,
            engine_fingerprint="engine", output=str(bundle),
        ))
        incremental.run_incremental(args)
        [book_file] = (self.tmp / "repo" / "line-baseline" / "books").iterdir()
        return plan, json.loads(book_file.read_text(encoding="utf-8"))

    def test_second_cycle_probes_the_author_line_and_keeps_flags(self):
        _plan, first = self.run_cycle(OLD, "first")
        self.assertTrue(first["book_context"])
        self.assertEqual([row[2] for row in first["lines"]],
                         [OLD_ENTITIES[i] for i in range(7)])
        plan, second = self.run_cycle(_with_line(OLD, 1, "מחבר חדש"), "second")
        [item] = plan["changed"]
        self.assertEqual(item["ner_ranges"], [[1, 2]])
        self.assertEqual(item["context_ranges"], [[0, 7]])
        self.assertEqual(self.payloads[-1][0]["context_ranges"], [[0, 7]])
        self.assertEqual([row[2] for row in second["lines"]],
                         [OLD_ENTITIES[i] for i in range(7)])
        self.assertEqual(second["ibid_seams"], _seams(OLD, OLD_ENTITIES))
        self.assertEqual(first["ibid_seams"], second["ibid_seams"])


if __name__ == "__main__":
    unittest.main()
