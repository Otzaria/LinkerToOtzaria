"""A stateful-ibid book must survive a recycle without replaying itself.

Sefaria's ibid history is what resolves a bare "שם", so a book carrying one must
see the same history at every batch seam whether or not a worker was recycled in
between.  Until 2026-09-20 that was guaranteed the expensive way: every shard was
deleted and the book replayed from batch 0, which meant a book whose peak exceeded
the recycle cap made zero progress per life.  Relinks 35458432836 and 35477368314
both spun there for hours.  The history now travels with each shard instead.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import link_books  # noqa: E402


class Ref:
    """Just enough of a Sefaria Ref: a normal form that round-trips."""

    def __init__(self, normal):
        self._normal = normal

    def normal(self):
        return self._normal

    def __eq__(self, other):
        return isinstance(other, Ref) and other._normal == self._normal

    def __repr__(self):
        return f"Ref({self._normal!r})"


class _History:
    def __init__(self):
        self._refs = []

    @property
    def last_refs(self):
        return self._refs

    @last_refs.setter
    def last_refs(self, ref):
        self._refs.append(ref)
        self._refs = self._refs[-3:]


class _Resolver:
    def __init__(self):
        self._ibid_history = _History()

    def reset_ibid_history(self):
        self._ibid_history = _History()


class _RawEntity:
    def __init__(self, span_range):
        self.span = type("Span", (), {"range": span_range})()
        self.raw_ref_parts = []


class _ResolvedRef:
    """One unambiguous citation covering the first two characters of a line."""

    is_ambiguous = False

    def __init__(self, ref):
        self._ref = ref
        self.raw_entity = _RawEntity((0, 2))
        self.resolved_raw_refs = []

    @property
    def ref(self):
        return self._ref


class _Linker:
    """Records the ibid history each bulk_link call starts from, and emits a link."""

    def __init__(self):
        self._ref_resolver = _Resolver()
        self.seen = []

    def bulk_link(self, texts, book_context_refs=None, type_filter=None):
        self._ref_resolver.reset_ibid_history()
        self.seen.append([ref.normal() for ref in self._ref_resolver._ibid_history.last_refs])
        docs = []
        for text in texts:
            ref = Ref(text)
            self._ref_resolver._ibid_history.last_refs = ref
            docs.append(type("Doc", (), {"resolved_refs": [_ResolvedRef(ref)]})())
        return docs


class _Recycled(Exception):
    pass


BOOK = link_books.BookKey("source", "ibid-book")
LINES = [(i, f"שורה {i} שם", "ספר") for i in range(6)]


def _run(linker, checkpoint, output, *, recycle_after=None, log=None):
    """One worker life over BOOK; `recycle_after` batches raises _Recycled."""
    calls = {"batches": 0}

    def on_recycle(_rss, _batches):
        raise _Recycled()

    def memory():
        # Grow one step per measurement so the cap trips right after batch N.
        calls["batches"] += 1
        if recycle_after is None:
            return 1e6
        return 1e6 if calls["batches"] <= recycle_after else 9e9

    with mock.patch.object(link_books, "BATCH_LINES", 2), \
            mock.patch.object(link_books, "HEAVY_BOOK_GROWTH_BYTES", 0), \
            mock.patch.object(link_books, "RSS_CAP", 1e9), \
            mock.patch.object(link_books, "worker_memory_bytes", side_effect=memory):
        return link_books.process_book_checkpointed(
            linker, BOOK, LINES, (log or (lambda _line: None)), lambda: None,
            checkpoint, output, on_recycle,
            context_ref_factory=Ref,
            ibid_context=link_books.IbidContext(),
            defer_when_heavy=False,
        )


class IbidCheckpointTest(unittest.TestCase):
    def test_a_recycled_book_resumes_with_the_history_of_an_unbroken_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            whole = _Linker()
            _run(whole, os.path.join(tmp, "a"), os.path.join(tmp, "a.jsonl"))
            self.assertEqual(len(whole.seen), 3)

            checkpoint = os.path.join(tmp, "b")
            output = os.path.join(tmp, "b.jsonl")
            first = _Linker()
            with self.assertRaises(_Recycled):
                _run(first, checkpoint, output, recycle_after=1)
            self.assertEqual(first.seen, whole.seen[:1])

            # The fresh image continues at batch 1 with batch 0's history, and never
            # re-links a batch it already committed.
            second = _Linker()
            _run(second, checkpoint, output)
            self.assertEqual(second.seen, whole.seen[1:])
            self.assertTrue(os.path.exists(output))

    def test_every_shard_carries_the_history_that_produced_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = os.path.join(tmp, "c")
            _run(_Linker(), checkpoint, os.path.join(tmp, "c.jsonl"))
            shards = sorted(f for f in os.listdir(checkpoint) if f.endswith(".jsonl"))
            self.assertEqual(len(shards), 3)
            for shard in shards:
                sidecar = os.path.join(checkpoint, shard[: -len(".jsonl")] + ".ibid.json")
                self.assertTrue(os.path.exists(sidecar), f"{shard} has no ibid state")
                with open(sidecar, encoding="utf-8") as stream:
                    state = json.load(stream)
                self.assertEqual(state["schema"], link_books.IbidContext.IBID_STATE_SCHEMA)
                self.assertTrue(all(isinstance(ref, str) for ref in state["resolved_refs"]))

    def test_an_unusable_ibid_state_replays_that_batch_and_everything_after_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = os.path.join(tmp, "d")
            output = os.path.join(tmp, "d.jsonl")
            whole = _Linker()
            _run(whole, checkpoint, output)
            os.remove(output)
            # Batch 1's history is gone; batch 2's shard was written under a history
            # this life can no longer reproduce, so both must be linked again.
            os.remove(os.path.join(checkpoint, "000000000002.ibid.json"))
            logged = []
            again = _Linker()
            _run(again, checkpoint, output, log=logged.append)
            self.assertEqual(again.seen, whole.seen[1:])
            self.assertEqual(len(logged), 1)
            self.assertIn("ibid-state", logged[0])

    def test_without_a_ref_factory_the_book_still_replays_from_its_first_batch(self):
        # No factory, no rehydrated history: correctness wins over progress.
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = os.path.join(tmp, "e")
            output = os.path.join(tmp, "e.jsonl")
            _run(_Linker(), checkpoint, output)
            os.remove(output)
            again = _Linker()
            with mock.patch.object(link_books, "BATCH_LINES", 2), \
                    mock.patch.object(link_books, "HEAVY_BOOK_GROWTH_BYTES", 0), \
                    mock.patch.object(link_books, "RSS_CAP", 1e9), \
                    mock.patch.object(link_books, "worker_memory_bytes", return_value=1e6):
                link_books.process_book_checkpointed(
                    again, BOOK, LINES, lambda _line: None, lambda: None,
                    checkpoint, output, lambda *_a: self.fail("unexpected recycle"),
                    context_ref_factory=None,
                    ibid_context=link_books.IbidContext(),
                    defer_when_heavy=False,
                )
            self.assertEqual(len(again.seen), 3)
            self.assertEqual(again.seen[0], [])

    def test_a_state_written_for_another_shard_is_refused(self):
        # The sidecar that matters most is the one that looks plausible: same book,
        # valid JSON, wrong batch. Adopting it would change output with no error.
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = os.path.join(tmp, "f")
            output = os.path.join(tmp, "f.jsonl")
            whole = _Linker()
            _run(whole, checkpoint, output)
            os.remove(output)
            first = os.path.join(checkpoint, "000000000000.ibid.json")
            with open(first, encoding="utf-8") as stream:
                stolen = json.load(stream)
            with open(os.path.join(checkpoint, "000000000002.ibid.json"), "w",
                      encoding="utf-8") as stream:
                json.dump(stolen, stream)
            logged = []
            again = _Linker()
            _run(again, checkpoint, output, log=logged.append)
            self.assertEqual(again.seen, whole.seen[1:])
            self.assertEqual(len(logged), 1)
            self.assertIn("different batch", logged[0])

    def test_a_shard_edited_after_its_state_was_written_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = os.path.join(tmp, "g")
            output = os.path.join(tmp, "g.jsonl")
            whole = _Linker()
            _run(whole, checkpoint, output)
            os.remove(output)
            shard = os.path.join(checkpoint, "000000000002.jsonl")
            with open(shard, "a", encoding="utf-8") as stream:
                stream.write("\n")
            logged = []
            _run(_Linker(), checkpoint, output, log=logged.append)
            self.assertEqual(len(logged), 1)
            self.assertIn("does not match its shard", logged[0])

    def test_a_ref_factory_that_raises_anything_replays_instead_of_failing(self):
        class Angry:
            def __init__(self, _normal):
                raise Exception("pinned Sefaria InputError is a plain Exception")

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = os.path.join(tmp, "h")
            output = os.path.join(tmp, "h.jsonl")
            _run(_Linker(), checkpoint, output)
            os.remove(output)
            logged = []
            again = _Linker()
            with mock.patch.object(link_books, "BATCH_LINES", 2), \
                    mock.patch.object(link_books, "HEAVY_BOOK_GROWTH_BYTES", 0), \
                    mock.patch.object(link_books, "RSS_CAP", 1e9), \
                    mock.patch.object(link_books, "worker_memory_bytes", return_value=1e6):
                link_books.process_book_checkpointed(
                    again, BOOK, LINES, logged.append, lambda: None,
                    checkpoint, output, lambda *_a: self.fail("unexpected recycle"),
                    context_ref_factory=Angry,
                    ibid_context=link_books.IbidContext(),
                    defer_when_heavy=False,
                )
            self.assertEqual(len(again.seen), 3, "the book must replay, not fail")

    def test_the_durable_local_cache_accepts_the_sidecar(self):
        # The periodic saver refuses any unexpected member, so an unlisted sidecar
        # would silently disable the durable checkpoint and fail the job at exit.
        sys.path.insert(0, os.path.join(ROOT, "ci"))
        import local_checkpoint_cache  # noqa: E402

        with tempfile.TemporaryDirectory() as tmp:
            book = os.path.join(tmp, "checkpoints", "a" * 40)
            os.makedirs(book)
            with open(os.path.join(tmp, "changed_books.json"), "w", encoding="utf-8") as stream:
                stream.write("[]\n")
            for name in ("000000000000.jsonl", "000000000000.ibid.json"):
                with open(os.path.join(book, name), "w", encoding="utf-8") as stream:
                    stream.write("{}\n")
            listed = {entry["path"] for entry in local_checkpoint_cache.listed_files(
                __import__("pathlib").Path(tmp))}
            self.assertIn(f"checkpoints/{'a' * 40}/000000000000.ibid.json", listed)

    def test_state_round_trips_through_its_serialized_form(self):
        context = link_books.IbidContext()
        context.resolved_refs = (Ref("Genesis 1:1"), Ref("Genesis 1:2"))
        context.last_emitted_ref = Ref("Genesis 1:2")
        context.initialized = True
        batch = [(4, "שורה 4 שם", "ספר"), (5, "שורה 5 שם", "ספר")]
        state = context.state(BOOK, 4, batch, "d" * 64)
        restored = link_books.IbidContext()
        restored.restore_state(state, Ref, BOOK, 4, batch, "d" * 64)
        self.assertEqual(list(restored.resolved_refs), list(context.resolved_refs))
        self.assertEqual(restored.last_emitted_ref, context.last_emitted_ref)
        self.assertTrue(restored.initialized)
        for broken in ({"schema": 99}, {**state, "resolved_refs": "x"},
                       {**state, "last_emitted_ref": 7},
                       {**state, "book": ["other", "book"]},
                       {**state, "batch_start": 6},
                       {**state, "batch_extent": [1, 4]},
                       {**state, "shard_sha256": "e" * 64}):
            with self.assertRaises(RuntimeError):
                link_books.IbidContext().restore_state(broken, Ref, BOOK, 4, batch, "d" * 64)
        with self.assertRaises(RuntimeError):
            link_books.IbidContext().restore_state(state, None, BOOK, 4, batch, "d" * 64)

    def test_a_rejected_state_leaves_no_half_restored_history(self):
        def factory(normal):
            if normal == "Genesis 1:9":
                raise ValueError("unresolvable antecedent")
            return Ref(normal)

        context = link_books.IbidContext()
        context.resolved_refs = (Ref("Genesis 1:1"),)
        context.last_emitted_ref = Ref("Genesis 1:1")
        context.initialized = True
        poisoned = {
            **link_books.IbidContext().state(BOOK, 0, [(0, "שורה", "ספר")], "d" * 64),
            "resolved_refs": ["Genesis 2:2"],
            "last_emitted_ref": "Genesis 1:9",
        }
        with self.assertRaises(ValueError):
            context.restore_state(
                poisoned, factory, BOOK, 0, [(0, "שורה", "ספר")], "d" * 64
            )
        self.assertEqual(list(context.resolved_refs), [Ref("Genesis 1:1")])
        self.assertEqual(context.last_emitted_ref, Ref("Genesis 1:1"))


if __name__ == "__main__":
    unittest.main()
