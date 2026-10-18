import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import link_books  # noqa: E402


class WorkerMemoryTest(unittest.TestCase):
    def test_transport_batches_bound_lines_and_characters_without_dropping_giant_line(self):
        lines = [
            (0, "אבג", None),
            (1, "דהו", None),
            (2, "ז" * 12, None),
            (3, "חטי", None),
        ]
        with mock.patch.object(link_books, "BATCH_LINES", 3), \
                mock.patch.object(link_books, "BATCH_CHARS", 7):
            batches = list(link_books.transport_batches(lines))
        self.assertEqual(
            [(start, [row[0] for row in batch]) for start, batch in batches],
            [(0, [0, 1]), (2, [2]), (3, [3])],
        )

    def test_periodic_heartbeat_runs_during_one_blocked_batch(self):
        calls = []
        with link_books.periodic_heartbeat(lambda: calls.append(time.monotonic()), interval=0.01):
            time.sleep(0.035)
        self.assertGreaterEqual(len(calls), 3)

    def test_cpu_resolver_starts_largest_books_first(self):
        books = [link_books.BookKey("s", "small"), link_books.BookKey("s", "large")]
        self.assertEqual(
            [
                book.canonical_he_title
                for book in link_books.largest_books_first(
                    books, {("s", "small"): 1, ("s", "large"): 4}
                )
            ],
            ["large", "small"],
        )

    def test_cooperative_batch_claim_is_kernel_exclusive_and_resumable(self):
        class Doc:
            resolved_refs = []

        class Linker:
            def bulk_link(self, texts, book_context_refs=None, type_filter=None, **_):
                return [Doc() for _ in texts]

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = os.path.join(tmp, "checkpoint")
            os.makedirs(checkpoint)
            first = link_books.BatchClaim.acquire(checkpoint, 0)
            self.assertIsNotNone(first)
            self.assertIsNone(link_books.BatchClaim.acquire(checkpoint, 0))
            with self.assertRaises(link_books.BookWorkInProgress):
                link_books.process_book_checkpointed(
                    Linker(), link_books.BookKey("s", "b"), [(0, "אב", None)],
                    lambda _line: None, lambda: None, checkpoint,
                    os.path.join(tmp, "out.jsonl"), lambda *_args: None,
                    cooperative=True,
                    prior_lock_path=os.path.join(tmp, "prior.lock"),
                )
            first.release()
            count, _words = link_books.process_book_checkpointed(
                Linker(), link_books.BookKey("s", "b"), [(0, "אב", None)],
                lambda _line: None, lambda: None, checkpoint,
                os.path.join(tmp, "out.jsonl"), lambda *_args: None,
                cooperative=True,
                prior_lock_path=os.path.join(tmp, "prior.lock"),
            )
            self.assertEqual(count, 0)

    def test_cooperative_reuse_captures_prior_artifact_before_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = os.path.join(tmp, "checkpoint")
            output = os.path.join(tmp, "artifact.jsonl")
            os.makedirs(tmp, exist_ok=True)
            with open(output, "w", encoding="utf-8") as stream:
                stream.write("old\n")
            captured = link_books.capture_prior_artifact(
                checkpoint, output, os.path.join(tmp, "claim", "prior.lock")
            )
            with open(output, "w", encoding="utf-8") as stream:
                stream.write("new\n")
            self.assertEqual(
                link_books.capture_prior_artifact(
                    checkpoint, output, os.path.join(tmp, "claim", "prior.lock")
                ),
                captured,
            )
            with open(captured, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "old\n")

    def test_snapshot_contract_requires_context_schema_and_policy(self):
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE lines_snapshot("
            "source_name TEXT, canonical_he_title TEXT, line_index INTEGER, "
            "content TEXT, context_ref TEXT)"
        )
        connection.execute(
            "CREATE TABLE lines_snapshot_meta(key TEXT PRIMARY KEY, value TEXT)"
        )
        connection.executemany(
            "INSERT INTO lines_snapshot_meta VALUES(?,?)",
            [("schema_version", "2"), ("context_policy", "explicit-relative-v1")],
        )
        link_books.validate_snapshot_contract(connection)
        connection.execute(
            "UPDATE lines_snapshot_meta SET value='1' WHERE key='schema_version'"
        )
        with self.assertRaisesRegex(RuntimeError, "snapshot must be schema 2"):
            link_books.validate_snapshot_contract(connection)
        connection.close()

    def test_linux_rss_uses_current_proc_value_not_inherited_high_water_mark(self):
        # Linux keeps ru_maxrss across execve.  A recycled worker must ignore that
        # historical maximum and use the current resident pages from /proc instead.
        high_water = mock.Mock(ru_maxrss=9_999_999)
        with mock.patch.object(link_books.sys, "platform", "linux"), \
                mock.patch("builtins.open", mock.mock_open(read_data="100 7 0 0 0 0 0\n")), \
                mock.patch.object(link_books.os, "sysconf", return_value=4096), \
                mock.patch.object(link_books.resource, "getrusage", return_value=high_water):
            self.assertEqual(link_books.rss_bytes(), 7 * 4096)

    def test_zero_progress_over_cap_fails_instead_of_exec_loop(self):
        with self.assertRaisesRegex(RuntimeError, "zero-progress recycle loop"):
            link_books.recycle_needed(current_rss=101, cap=100, processed=0)

    def test_recycle_only_after_progress(self):
        self.assertFalse(link_books.recycle_needed(current_rss=100, cap=100, processed=0))
        self.assertTrue(link_books.recycle_needed(current_rss=101, cap=100, processed=1))

    def test_stale_heartbeat_cannot_steal_a_live_book_lock(self):
        """A slow resolver must never race a peer over its checkpoint directory."""
        with tempfile.TemporaryDirectory() as run:
            os.makedirs(os.path.join(run, "done"))
            first = link_books.BookClaim.acquire(run, "book", stale_seconds=1)
            self.assertIsNotNone(first)
            heartbeat = os.path.join(run, "claim", "book", "hb")
            old = time.time() - 2
            os.utime(heartbeat, (old, old))

            # Heartbeat age alone used to permit this second owner.  The kernel
            # lock proves the first worker is still alive, so no takeover occurs.
            self.assertIsNone(link_books.BookClaim.acquire(run, "book", stale_seconds=1))
            self.assertTrue(os.path.isdir(os.path.join(run, "claim", "book")))

            first.release()
            recovered = link_books.BookClaim.acquire(run, "book", stale_seconds=1)
            self.assertIsNotNone(recovered)
            recovered.release()

    def test_fresh_orphan_claim_is_recovered_immediately(self):
        """A dead owner is proved by the free kernel lock; heartbeat age is irrelevant."""
        with tempfile.TemporaryDirectory() as run:
            os.makedirs(os.path.join(run, "done"))
            claim_dir = os.path.join(run, "claim", "book")
            os.makedirs(claim_dir)
            open(os.path.join(claim_dir, "hb"), "w").close()

            recovered = link_books.BookClaim.acquire(run, "book", stale_seconds=900)
            self.assertIsNotNone(recovered)
            recovered.release()

    def test_claim_heartbeat_never_recreates_lost_ownership(self):
        with tempfile.TemporaryDirectory() as run:
            os.makedirs(os.path.join(run, "done"))
            claim = link_books.BookClaim.acquire(run, "book")
            self.assertIsNotNone(claim)
            os.remove(os.path.join(run, "claim", "book", "hb"))
            with self.assertRaisesRegex(RuntimeError, "claim disappeared"):
                claim.heartbeat()
            claim.release()

    def test_checkpointed_book_resumes_after_mid_book_recycle(self):
        class Doc:
            resolved_refs = []

        class Linker:
            def __init__(self):
                self.calls = []

            def bulk_link(self, texts, book_context_refs=None, type_filter=None, **_):
                self.calls.append((list(texts), list(book_context_refs or []), type_filter))
                return [Doc() for _ in texts]

        class RecycleSignal(Exception):
            pass

        bk = link_books.BookKey("source", "giant")
        lines = [(i, f"שורה {i}", "ספר") for i in range(5)]
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = os.path.join(tmp, "checkpoint")
            output = os.path.join(tmp, "artifacts", "giant.jsonl")
            first = Linker()
            with mock.patch.object(link_books, "BATCH_LINES", 2), \
                    mock.patch.object(link_books, "RSS_CAP", 100), \
                    mock.patch.object(link_books, "rss_bytes", return_value=101):
                with self.assertRaises(RecycleSignal):
                    link_books.process_book_checkpointed(
                        first, bk, lines, lambda _line: None, lambda: None,
                        checkpoint, output,
                        lambda _rss, _batches: (_ for _ in ()).throw(RecycleSignal()),
                    )
            self.assertEqual(first.calls, [(["שורה 0", "שורה 1"], [None, None], "citation")])
            self.assertTrue(os.path.exists(os.path.join(checkpoint, "000000000000.jsonl")))
            self.assertFalse(os.path.exists(output), "partial book became publicly visible")

            second = Linker()
            with mock.patch.object(link_books, "BATCH_LINES", 2), \
                    mock.patch.object(link_books, "RSS_CAP", 100), \
                    mock.patch.object(link_books, "rss_bytes", return_value=0):
                count, words = link_books.process_book_checkpointed(
                    second, bk, lines, lambda _line: None, lambda: None,
                    checkpoint, output,
                    lambda _rss, _batches: self.fail("unexpected second recycle"),
                )
            self.assertEqual(second.calls, [
                (["שורה 2", "שורה 3"], [None, None], "citation"),
                (["שורה 4"], [None], "citation"),
            ])
            self.assertEqual(count, 0)
            self.assertEqual(words, 10)
            self.assertTrue(os.path.exists(output))

    def test_resumed_checkpoint_is_bound_to_exact_source_content(self):
        bk = link_books.BookKey("source", "book")
        lines = [(0, "תוכן נוכחי", "ספר")]
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = os.path.join(tmp, "checkpoint")
            os.makedirs(checkpoint)
            link_books.write_artifact(
                os.path.join(checkpoint, "000000000000.jsonl"),
                [
                    link_books.LinkRecord(
                        bk, 0, 0, 1, "Genesis 1:1", source_hash="0" * 16
                    )
                ],
            )
            with self.assertRaisesRegex(RuntimeError, "source hash mismatch"):
                link_books.process_book_checkpointed(
                    object(), bk, lines, lambda _line: None, lambda: None,
                    checkpoint, os.path.join(tmp, "artifact.jsonl"),
                    lambda *_args: None,
                )

    def test_heading_lines_are_not_sent_and_context_is_forwarded(self):
        class Doc:
            resolved_refs = []

        class Linker:
            def __init__(self):
                self.calls = []

            def bulk_link(self, texts, book_context_refs=None, type_filter=None, **_):
                self.calls.append((list(texts), list(book_context_refs), type_filter))
                return [Doc() for _ in texts]

        linker = Linker()
        records, words = link_books.process_book(
            linker,
            link_books.BookKey("source", "ספר"),
            [
                (0, "<h1>ספר</h1>", "ספר"),
                (1, "ראו לקמן משנה א", "ברכות א, א"),
            ],
            lambda _line: None,
            lambda: None,
            context_ref_factory=lambda value: f"REF:{value}",
        )
        self.assertEqual(records, [])
        self.assertEqual(words, 4)
        self.assertEqual(
            linker.calls,
            [(["ראו לקמן משנה א"], ["REF:ברכות א, א"], "citation")],
        )

    def test_ibid_history_survives_transport_batch_boundaries(self):
        class Ref:
            def __init__(self, text):
                self.text = text

            def normal(self):
                return self.text

        class History:
            def __init__(self):
                self._refs = []

            @property
            def last_refs(self):
                return self._refs

            @last_refs.setter
            def last_refs(self, ref):
                self._refs.append(ref)
                self._refs = self._refs[-3:]

        class Resolver:
            def __init__(self):
                self._ibid_history = History()

            def reset_ibid_history(self):
                self._ibid_history = History()

        class Linker:
            def __init__(self):
                self._ref_resolver = Resolver()
                self.seen = []

            def bulk_link(self, texts, book_context_refs=None, type_filter=None, **_):
                self._ref_resolver.reset_ibid_history()
                self.seen.append([ref.normal() for ref in self._ref_resolver._ibid_history.last_refs])
                self._ref_resolver._ibid_history.last_refs = Ref(texts[0])
                return [type("Doc", (), {"resolved_refs": []})() for _ in texts]

        linker = Linker()
        context = link_books.IbidContext()
        context.resolve(linker, lambda: linker.bulk_link(["Genesis 30:1"]))
        context.resolve(linker, lambda: linker.bulk_link(["Genesis 30:2"]))
        self.assertEqual(linker.seen, [[], ["Genesis 30:1"]])

    def test_non_prose_payloads_are_not_sent_to_hebrew_ner(self):
        self.assertFalse(link_books.is_ner_eligible_line("<img src='data:image/png;base64,AAAA'>"))
        self.assertFalse(link_books.is_ner_eligible_line("English metadata only"))
        self.assertTrue(link_books.is_ner_eligible_line("<b>ראו</b> ברכות ב ע״א"))

    def test_relative_policy_requires_direction_location_and_explicit_cross_book(self):
        class Kind:
            def __init__(self, name):
                self.name = name

        class Part:
            def __init__(self, name):
                self.type = Kind(name)

        class Index:
            def __init__(self, title):
                self.title = title

        class Ref:
            def __init__(self, title, order):
                self.index = Index(title)
                self._order = order

            def order_id(self):
                return self._order

        class Raw:
            def __init__(self, names):
                self.raw_ref_parts = [Part(name) for name in names]

        class RR:
            is_ambiguous = False

            def __init__(self, names):
                self.raw_entity = Raw(names)
                self.context_type = Kind("CURRENT_BOOK")

        source = Ref("Book", "002")
        self.assertEqual(
            link_books.relative_ref_direction(
                RR(["RELATIVE", "NUMBERED"]), Ref("Book", "003"), source, "לקמן בסימן ג"
            ),
            "below",
        )
        self.assertIsNone(link_books.relative_ref_direction(
            RR(["RELATIVE", "NUMBERED"]), Ref("Book", "001"), source, "לקמן בסימן א"
        ))
        self.assertIsNone(link_books.relative_ref_direction(
            RR(["RELATIVE", "NUMBERED"]), Ref("Other", "003"), source, "לקמן בסימן ג"
        ))
        self.assertEqual(
            link_books.relative_ref_direction(
                RR(["RELATIVE", "NAMED", "NUMBERED"]), Ref("Other", "001"), source,
                "לקמן בספר אחר סימן א",
            ),
            "below",
        )
        self.assertIsNone(link_books.relative_ref_direction(
            RR(["RELATIVE"]), Ref("Book", "001"), source, "כמו שכתבנו לעיל"
        ))

    def test_context_cannot_create_an_ordinary_non_relative_link(self):
        class Kind:
            def __init__(self, name):
                self.name = name

        class RR:
            is_ambiguous = False
            ref = object()
            context_type = Kind("CURRENT_BOOK")
            raw_entity = type("Raw", (), {"raw_ref_parts": []})()

        self.assertIsNone(link_books._pick_ref(RR()))

    def test_explicit_relative_record_carries_exact_context_and_direction(self):
        class Kind:
            def __init__(self, name):
                self.name = name

        class Part:
            def __init__(self, name):
                self.type = Kind(name)

        class Index:
            title = "Book"

        class Ref:
            index = Index()

            def __init__(self, order, normalized):
                self.order = order
                self.normalized = normalized

            def order_id(self):
                return self.order

            def normal(self):
                return self.normalized

        class Span:
            range = (4, 17)

        class Raw:
            span = Span()
            raw_ref_parts = [Part("RELATIVE"), Part("NUMBERED")]

        class RR:
            is_ambiguous = False
            context_type = Kind("CURRENT_BOOK")
            raw_entity = Raw()
            ref = Ref("003", "Book 3")

        class Doc:
            resolved_refs = [RR()]

        class Linker:
            def bulk_link(self, texts, book_context_refs=None, type_filter=None, **_):
                self.contexts = book_context_refs
                return [Doc()]

        source_ref = Ref("002", "Book 2")
        linker = Linker()
        records, _ = link_books.process_batch(
            linker,
            link_books.BookKey("source", "ספר"),
            [(7, "ראו לקמן בסימן ג", "Book 2")],
            lambda _line: None,
            context_ref_factory=lambda _value: source_ref,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].context_ref, "Book 2")
        self.assertEqual(records[0].relative_direction, "below")
        self.assertEqual(records[0].target_ref, "Book 3")

    def test_unresolved_own_tractate_relative_citation_is_recovered(self):
        """Sefaria returns "לקמן גיטין דף כג:" as a failure (Otzaria/otzaria#2408)."""
        from test_citation_plausibility import TalmudRef

        Kind = lambda name: type("Kind", (), {"name": name})()  # noqa: E731

        def failed(start, end, parts=("RELATIVE", "NAMED", "NUMBERED")):
            return type("RR", (), {
                "is_ambiguous": False, "ref": None, "context_type": None,
                "resolution_failed": True,
                "raw_entity": type("Raw", (), {
                    "span": type("Span", (), {"range": (start, end)})(),
                    "raw_ref_parts": [type("Part", (), {"type": Kind(p)})() for p in parts],
                })(),
            })()

        content = "עיין (לקמן גיטין דף כג:) ו(לקמן ברכות דף כג:)"
        own = content.index("לקמן גיטין")
        other = content.index("לקמן ברכות")

        class Linker:
            def bulk_link(self, texts, book_context_refs=None, type_filter=None,
                          with_failures=False):
                self.with_failures = with_failures
                return [type("Doc", (), {"resolved_refs": [
                    failed(own, own + len("לקמן גיטין דף כג:")),
                    failed(other, other + len("לקמן ברכות דף כג:")),
                ]})()]

        linker = Linker()
        records, _ = link_books.process_batch(
            linker, link_books.BookKey("Sefaria", "רש\"י על גיטין"),
            [(7, content, "Rashi on Gittin 3a:14:1")], lambda _line: None,
            context_ref_factory=TalmudRef,
        )
        self.assertTrue(linker.with_failures)
        self.assertEqual(
            [(r.start, r.target_ref, r.relative_direction, r.context_ref) for r in records],
            [(own, "Gittin 23b", "below", "Rashi on Gittin 3a:14:1")],
        )

    def test_unresolved_relative_citations_are_recovered_outside_ibid_history(self):
        """Tosafot's "(לקמן יא:)" fails in Sefaria; recovering it must not move "שם"."""
        from test_citation_plausibility import TalmudRef

        Kind = lambda name: type("Kind", (), {"name": name})()  # noqa: E731

        def failed(start, end):
            return type("RR", (), {
                "is_ambiguous": False, "ref": None, "context_type": None,
                "resolution_failed": True,
                "raw_entity": type("Raw", (), {
                    "span": type("Span", (), {"range": (start, end)})(),
                    "raw_ref_parts": [type("Part", (), {"type": Kind(p)})()
                                      for p in ("RELATIVE", "NUMBERED")],
                })(),
            })()

        content = "עיין (לקמן יא:) ו(לקמן שם) ו(לעיל ע\"ב) ו(לקמן שבת דף כג:)"
        spans = [(content.index(a), content.index(a) + len(a))
                 for a in ("(לקמן יא:)", "(לקמן שם)", "(לעיל ע\"ב)", "(לקמן שבת דף כג:)")]

        class Linker:
            def bulk_link(self, texts, book_context_refs=None, type_filter=None, **_):
                return [type("Doc", (), {"resolved_refs": [failed(*s) for s in spans]})()]

        previous = TalmudRef("Shabbat 2a")
        ibid = type("Ibid", (), {"last_emitted_ref": previous,
                                 "resolve": lambda _self, _linker, invoke: invoke()})()
        records, _ = link_books.process_batch(
            Linker(), link_books.BookKey("Sefaria", "תוספות על שבת"),
            [(7, content, "Tosafot on Shabbat 3a:1:1")], lambda _line: None,
            context_ref_factory=TalmudRef, ibid_context=ibid,
        )
        self.assertEqual(
            [(r.start, r.target_ref, r.relative_direction, r.context_ref) for r in records],
            [(spans[0][0], "Shabbat 11b", "below", "Tosafot on Shabbat 3a:1:1"),
             (spans[3][0], "Shabbat 23b", "below", "Tosafot on Shabbat 3a:1:1")],
        )
        self.assertIs(ibid.last_emitted_ref, previous)

    def test_unresolved_geresh_numbers_are_recovered_outside_ibid_history(self):
        """"(תהלים יח' לא')" fails in Sefaria; it links, while other failures stay dropped."""
        from test_citation_plausibility import TanakhRef

        def failed(start, end):
            return type("RR", (), {
                "is_ambiguous": False, "ref": None, "context_type": None,
                "resolution_failed": True,
                "raw_entity": type("Raw", (), {
                    "span": type("Span", (), {"range": (start, end)})(),
                    "raw_ref_parts": [],
                })(),
            })()

        anchors = ("(תהלים יח' לא')", '(תהלים י"ח לא\')', '(תהלים יח\' ל״א)',
                   "(שם כו')", "(תהלים יח לא)")
        content = "😀 אמרת יי צרופה " + " ".join(anchors)
        spans = [(content.index(a), content.index(a) + len(a)) for a in anchors]

        class Linker:
            def bulk_link(self, texts, book_context_refs=None, type_filter=None, **_):
                return [type("Doc", (), {"resolved_refs": [failed(*s) for s in spans]})()]

        previous = TanakhRef("דברים ל ו")
        ibid = type("Ibid", (), {"last_emitted_ref": previous,
                                 "resolve": lambda _self, _linker, invoke: invoke()})()
        records, _ = link_books.process_batch(
            Linker(), link_books.BookKey("OnYourWayToOtzaria", "שבע נתיבות התורה"),
            [(4, content, "שבע נתיבות התורה 5")], lambda _line: None,
            context_ref_factory=TanakhRef, ibid_context=ibid,
        )
        self.assertEqual(
            [(r.start, r.end, r.target_ref, r.relative_direction, r.context_ref) for r in records],
            [(start + 1, end + 1, "Psalms 18:31", None, None) for start, end in spans[:3]],
        )
        self.assertTrue(all(r.source_hash == link_books.content_hash(content) for r in records))
        self.assertIs(ibid.last_emitted_ref, previous)

    def test_geresh_recovery_keeps_existing_successful_and_ambiguous_ibid_links(self):
        """Adding the failed Psalms citation must not change either Deuteronomy link."""
        from types import SimpleNamespace as NS
        from test_citation_plausibility import TanakhRef

        def ref(text):
            result = TanakhRef(text)
            result.index = NS(title="Deuteronomy")
            return result

        previous, continuation, other = [ref(text) for text in ("דברים ל ו", "דברים ל ז", "דברים כט ז")]
        anchors = ("דברים ל ו", '(תהלים י"ח לא\')', "שם שם ז")
        content = "עיין " + " ואז ".join(anchors)
        spans = [(content.index(a), content.index(a) + len(a)) for a in anchors]
        refs = [NS(
            is_ambiguous=i == 2, resolution_failed=i == 1,
            ref=previous if i == 0 else None, context_type=None,
            resolved_raw_refs=[NS(ref=continuation), NS(ref=other)] if i == 2 else [],
            raw_entity=NS(span=NS(range=span), raw_ref_parts=[
                NS(type=NS(name="IBID" if i == 2 else "NAMED"))]),
        ) for i, span in enumerate(spans)]
        linker = NS(bulk_link=lambda *_args, **_kwargs: [NS(resolved_refs=refs)])

        def run():
            ibid = NS(last_emitted_ref=None, resolve=lambda _linker, invoke: invoke())
            records, _ = link_books.process_batch(
                linker, link_books.BookKey("test", "test"), [(0, content, None)],
                lambda _line: None, context_ref_factory=TanakhRef, ibid_context=ibid,
            )
            return [r.to_dict() for r in records], ibid.last_emitted_ref

        with mock.patch.object(link_books, "geresh_numeral_ref", return_value=None):
            before, before_last = run()
        after, after_last = run()
        self.assertEqual(len(before), 2)
        self.assertEqual(len(after), 3)
        self.assertEqual(before, [r for r in after if r["target_ref"] != "Psalms 18:31"])
        self.assertIs(before_last, continuation)
        self.assertIs(after_last, continuation)

    def test_a_non_sefaria_talmud_commentary_recovers_by_its_daf_heading(self):
        """"(לקמן ד.)" in a Dicta "X על גיטין" line under "דף ג:" links to Gittin 4a."""
        from test_citation_plausibility import TalmudRef

        Kind = lambda name: type("Kind", (), {"name": name})()  # noqa: E731
        content = "עיין (לקמן ד.) ובבראשית"
        span = (content.index("(לקמן ד.)"), content.index("(לקמן ד.)") + len("(לקמן ד.)"))
        failed = type("RR", (), {
            "is_ambiguous": False, "ref": None, "context_type": None,
            "resolution_failed": True,
            "raw_entity": type("Raw", (), {
                "span": type("Span", (), {"range": span})(),
                "raw_ref_parts": [type("Part", (), {"type": Kind(p)})()
                                  for p in ("RELATIVE", "NUMBERED")],
            })(),
        })()

        class Linker:
            def __init__(self):
                self.contexts = []

            def bulk_link(self, texts, book_context_refs=None, type_filter=None, **_):
                self.contexts.append(list(book_context_refs))
                return [type("Doc", (), {"resolved_refs": [failed]})() for _ in texts]

        book = link_books.BookKey("DictaToOtzaria", "חידושי הר\"ן על גיטין")
        lines = [(0, "<h2>דף ג:</h2>", "חידושי הר\"ן על גיטין 1"),
                 (1, content, "חידושי הר\"ן על גיטין 2")]
        positions = link_books.talmud_positions(book, lines, TalmudRef)

        def run(talmud_positions):
            linker = Linker()
            previous = TalmudRef("Gittin 2a")
            ibid = type("Ibid", (), {"last_emitted_ref": previous,
                                     "resolve": lambda _self, _linker, invoke: invoke()})()
            records, _ = link_books.process_batch(
                linker, book, lines[1:], lambda _line: None, context_ref_factory=TalmudRef,
                ibid_context=ibid, talmud_positions=talmud_positions,
            )
            self.assertEqual(linker.contexts, [[None]])
            self.assertIs(ibid.last_emitted_ref, previous)
            return [(r.start, r.end, r.target_ref, r.relative_direction, r.context_ref)
                    for r in records]

        self.assertEqual(run(None), [])
        self.assertEqual(run(positions), [
            (*span, "Gittin 4a", "below", "חידושי הר\"ן על גיטין 2")])
        # Without its own context the record would be malformed for Phase 2.
        lines[1] = (1, content, "")
        self.assertEqual(run(positions), [])
        # A context Sefaria happens to parse never outranks the book's daf headings.
        lines[1] = (1, content, "Gittin 3a:1")
        records, _ = link_books.process_batch(
            Linker(), book, lines[1:], lambda _line: None, context_ref_factory=TalmudRef,
            talmud_positions={},
        )
        self.assertEqual(records, [])

    def test_book_drivers_pass_whole_book_talmud_positions_to_every_batch(self):
        from test_citation_plausibility import TalmudRef

        book = link_books.BookKey("DictaToOtzaria", "חידושי הר\"ן על גיטין")
        lines = [(0, "<h2>דף ג:</h2>", "c0"), (1, "שורה א", "c1"), (2, "שורה ב", "c2")]
        seen = []

        def fake_process(_linker, _bk, batch, *_args, talmud_positions=None, **_kwargs):
            seen.append((batch[0][0], {i: r.normal() for i, r in talmud_positions.items()}))
            return [], 0

        expected = {0: "Gittin 3b", 1: "Gittin 3b", 2: "Gittin 3b"}
        with mock.patch.object(link_books, "BATCH_LINES", 1), \
                mock.patch.object(link_books, "process_batch", side_effect=fake_process):
            link_books.process_book(None, book, lines, lambda _l: None, lambda: None,
                                    context_ref_factory=TalmudRef)
            self.assertEqual(seen, [(1, expected), (2, expected)])
            seen.clear()
            with tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(link_books, "RSS_CAP", 1e12), \
                    mock.patch.object(link_books, "worker_memory_bytes", return_value=1e9):
                link_books.process_book_checkpointed(
                    None, book, lines, lambda _l: None, lambda: None,
                    os.path.join(tmp, "cp"), os.path.join(tmp, "out.jsonl"),
                    lambda _rss, _b: self.fail("unexpected recycle"),
                    ner_indices={2}, reuse=[(1, 1), (0, 0)], context_ref_factory=TalmudRef,
                )
            self.assertEqual(seen, [(2, expected)])


if __name__ == "__main__":
    unittest.main()
