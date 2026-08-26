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

            def bulk_link(self, texts, book_context_refs=None, type_filter=None):
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

    def test_heading_lines_are_not_sent_and_context_is_forwarded(self):
        class Doc:
            resolved_refs = []

        class Linker:
            def __init__(self):
                self.calls = []

            def bulk_link(self, texts, book_context_refs=None, type_filter=None):
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
            def bulk_link(self, texts, book_context_refs=None, type_filter=None):
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


if __name__ == "__main__":
    unittest.main()
