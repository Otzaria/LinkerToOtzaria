"""A stateful-ibid book links the same under every transport layout.

Sefaria resolves a book's citations in one sequence of ibid history.  We cut the
book into transport batches (100 lines locally, 25 on Kaggle) and carry the history
across each seam; until ibid state schema 4 only its last refs crossed, so a title
cited before them ("Berakhot ... Shabbat, Shabbat, Shabbat ... שם #Berakhot") was
lost at a seam and the output depended on BATCH_LINES.
"""
import json
import os
import random
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import link_books  # noqa: E402
from linker_artifact import read_artifact  # noqa: E402
from test_ibid_checkpoint import Ref, _Resolver  # noqa: E402

BOOK = link_books.BookKey("Sefaria", "ספר פריסה")
CONTEXT = "ספר פריסה"
TITLES = ("Berakhot", "Shabbat", "Eruvin", "Pesachim", "Yoma")


class _ResolvedRef:
    def __init__(self, ref=None, candidates=()):
        self.ref = ref
        self.is_ambiguous = bool(candidates)
        self.resolved_raw_refs = [type("Candidate", (), {"ref": c})() for c in candidates]
        self.raw_entity = type("Raw", (), {
            "span": type("Span", (), {"range": (0, 2)})(), "raw_ref_parts": [],
        })()


class _Linker:
    """Sefaria's bulk_link over ";"-separated citations, one ibid history per call.

    "|T 2a" cites a ref, "#T" is "שם" by title (the title map), "@" a bare "שם"
    (the latest ref), "?T 2a/U 3a" an ambiguous citation (every candidate enters the
    history) and "!" a failed one, which resets the history as Sefaria does.
    """

    def __init__(self):
        self._ref_resolver = _Resolver()

    def bulk_link(self, texts, book_context_refs=None, type_filter=None, **_):
        resolver = self._ref_resolver
        resolver.reset_ibid_history()
        docs = []
        for text in texts:
            found = []
            for token in text.split(" ", 1)[1].split(";"):
                history = resolver._ibid_history
                if token.startswith("|"):
                    resolved = _ResolvedRef(Ref(token[1:]))
                elif token.startswith("#"):
                    ref = history.get_ref_by_title(token[1:])
                    resolved = _ResolvedRef(ref) if ref is not None else None
                elif token == "@":
                    refs = history.last_refs
                    resolved = _ResolvedRef(refs[-1]) if refs else None
                elif token.startswith("?"):
                    resolved = _ResolvedRef(candidates=[Ref(c) for c in token[1:].split("/")])
                else:
                    resolved = None
                # Sefaria's _update_ibid_history.
                if resolved is None:
                    resolver.reset_ibid_history()
                    continue
                if resolved.is_ambiguous:
                    for candidate in resolved.resolved_raw_refs:
                        resolver._ibid_history.last_refs = candidate.ref
                else:
                    resolver._ibid_history.last_refs = resolved.ref
                found.append(resolved)
            docs.append(type("Doc", (), {"resolved_refs": found})())
        return docs


class _Recycled(Exception):
    pass


def _link(tmp, name, lines, batch_lines, recycle_every=None):
    """Link a book whole, recycling the worker after every `recycle_every` batches."""
    checkpoint = os.path.join(tmp, f"{name}-checkpoints")
    out = os.path.join(tmp, f"{name}.jsonl")
    for _life in range(len(lines) + 2):
        batches = {"n": 0}

        def memory():
            batches["n"] += 1
            return 9e9 if recycle_every and batches["n"] > recycle_every else 1e6

        def on_recycle(_rss, _batches):
            raise _Recycled()

        with mock.patch.object(link_books, "BATCH_LINES", batch_lines), \
                mock.patch.object(link_books, "HEAVY_BOOK_GROWTH_BYTES", 0), \
                mock.patch.object(link_books, "RSS_CAP", 1e9), \
                mock.patch.object(link_books, "worker_memory_bytes", side_effect=memory):
            try:
                link_books.process_book_checkpointed(
                    _Linker(), BOOK, lines, lambda _line: None, lambda: None,
                    checkpoint, out, on_recycle, context_ref_factory=Ref,
                    ibid_context=link_books.IbidContext(), defer_when_heavy=False,
                )
            except _Recycled:
                continue
        return [(r.line_index, r.start, r.end, r.target_ref) for r in read_artifact(out)]
    raise AssertionError("the book never finished")


def _random_book(rng, length):
    lines = []
    for index in range(length):
        tokens = []
        for _ in range(rng.choice((0, 0, 1, 1, 2, 3))):
            kind = rng.random()
            title = rng.choice(TITLES)
            if kind < 0.45:
                tokens.append(f"|{title} {rng.randint(2, 9)}a")
            elif kind < 0.7:
                tokens.append(f"#{title}")
            elif kind < 0.85:
                tokens.append("@")
            elif kind < 0.93:
                other = rng.choice(TITLES)
                tokens.append(f"?{title} {rng.randint(2, 9)}a/{other} {rng.randint(2, 9)}b")
            else:
                tokens.append("!")
        lines.append((index, "מקור " + (";".join(tokens) or "-"), CONTEXT))
    return lines


class IbidLayoutTest(unittest.TestCase):
    def test_a_title_cited_before_the_last_three_refs_survives_a_seam(self):
        lines = [
            (0, "מקור |Berakhot 2a", CONTEXT),
            (1, "מקור |Shabbat 3a", CONTEXT),
            (2, "מקור |Shabbat 4a", CONTEXT),
            (3, "מקור |Shabbat 5a", CONTEXT),
            (4, "מקור #Berakhot", CONTEXT),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            whole = _link(tmp, "whole", lines, 100)
            self.assertIn((4, 0, 2, "Berakhot 2a"), whole)
            for batch_lines in (1, 2, 3, 4):
                with self.subTest(batch_lines=batch_lines):
                    self.assertEqual(_link(tmp, f"b{batch_lines}", lines, batch_lines), whole)

    def test_the_title_order_survives_a_seam(self):
        # _last_titles is in first-seen order, not recency: Berakhot is evicted
        # by the fourth title although it was cited last.
        lines = [
            (0, "מקור |Berakhot 2a", CONTEXT),
            (1, "מקור |Shabbat 3a", CONTEXT),
            (2, "מקור |Berakhot 4a", CONTEXT),
            (3, "מקור |Eruvin 5a", CONTEXT),
            (4, "מקור |Yoma 6a", CONTEXT),
            (5, "מקור #Shabbat;#Berakhot", CONTEXT),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            whole = _link(tmp, "whole", lines, 100)
            self.assertNotIn((5, 0, 2, "Berakhot 4a"), whole)
            self.assertIn((5, 0, 2, "Shabbat 3a"), whole)
            for batch_lines in (1, 2, 3, 4, 5):
                with self.subTest(batch_lines=batch_lines):
                    self.assertEqual(_link(tmp, f"b{batch_lines}", lines, batch_lines), whole)

    def test_random_books_link_the_same_under_every_layout_and_recycle(self):
        rng = random.Random(4)
        differences = []
        with tempfile.TemporaryDirectory() as tmp:
            for case in range(60):
                lines = _random_book(rng, rng.randint(4, 18))
                whole = _link(tmp, f"{case}-whole", lines, 1000)
                for batch_lines in (1, 2, 3, 5):
                    for recycle in (None, 1, 2):
                        got = _link(tmp, f"{case}-{batch_lines}-{recycle}", lines,
                                    batch_lines, recycle)
                        if got != whole:
                            differences.append((case, batch_lines, recycle))
        self.assertEqual(differences, [])

    def test_the_state_on_disk_holds_the_title_map(self):
        lines = [
            (0, "מקור |Berakhot 2a", CONTEXT),
            (1, "מקור |Shabbat 3a;|Shabbat 4a;|Shabbat 5a", CONTEXT),
            (2, "מקור #Berakhot", CONTEXT),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            _link(tmp, "disk", lines, 2)
            sidecar = os.path.join(tmp, "disk-checkpoints", "000000000000.ibid.json")
            with open(sidecar, encoding="utf-8") as stream:
                state = json.load(stream)
        self.assertEqual(state["schema"], 4)
        self.assertEqual(state["resolved_refs"], ["Shabbat 3a", "Shabbat 4a", "Shabbat 5a"])
        self.assertEqual(state["title_refs"], [["Berakhot", "Berakhot 2a"], ["Shabbat", "Shabbat 5a"]])


class IbidStateContractTest(unittest.TestCase):
    def state(self):
        context = link_books.IbidContext()
        context.resolved_refs = (Ref("Shabbat 3a"),)
        context.title_refs = (("Berakhot", Ref("Berakhot 2a")), ("Shabbat", Ref("Shabbat 3a")))
        context.initialized = True
        return context.state(BOOK, 0, [(0, "שורה", CONTEXT)], "d" * 64)

    def restore(self, state):
        context = link_books.IbidContext()
        context.restore_state(state, Ref, BOOK, 0, [(0, "שורה", CONTEXT)], "d" * 64)
        return context

    def test_the_title_map_round_trips_in_order(self):
        restored = self.restore(self.state())
        self.assertEqual(
            [(t, r.normal()) for t, r in restored.title_refs],
            [("Berakhot", "Berakhot 2a"), ("Shabbat", "Shabbat 3a")],
        )

    def test_a_schema_3_state_is_refused(self):
        # Written before the title map was carried: adopting it would drop titles.
        state = {**self.state(), "schema": 3}
        del state["title_refs"]
        with self.assertRaisesRegex(RuntimeError, "unrecognized"):
            self.restore(state)

    def test_a_malformed_title_map_is_refused(self):
        for broken in (None, "x", [["Berakhot"]], [["Berakhot", 2]],
                       [["Berakhot", "Berakhot 2a"], ["Berakhot", "Berakhot 3a"]]):
            with self.subTest(broken=broken):
                with self.assertRaisesRegex(RuntimeError, "title map"):
                    self.restore({**self.state(), "title_refs": broken})

    def test_a_history_beyond_the_pinned_bounds_is_refused(self):
        context = link_books.IbidContext()
        context.initialized = True
        context.title_refs = tuple((t, Ref(f"{t} 2a")) for t in TITLES[:4])
        linker = _Linker()
        with self.assertRaisesRegex(RuntimeError, "bounds"):
            context.resolve(linker, lambda: linker.bulk_link(["מקור -"]))

    def test_a_history_that_breaks_the_title_invariant_fails_loudly(self):
        linker = _Linker()

        def broken():
            linker._ref_resolver._ibid_history._last_titles = ["Berakhot", "Berakhot"]
            return []

        with self.assertRaisesRegex(RuntimeError, "invariant"):
            link_books.IbidContext().resolve(linker, broken)


if __name__ == "__main__":
    unittest.main()
